"""/authorize driven end-to-end with a URL-shaped client_id (Client ID Metadata Document).

Claude identifies itself to custom connectors this way instead of dynamic client
registration; the first real OAuth attempt died with a 500 because that key reached
Firestore unsanitized. These tests pin the contract against the in-memory backend:
a resolvable CIMD client ends in a redirect or the consent page, an unresolvable one
in a 4xx OAuth error, never a 5xx. See docs/DECISIONS.md for the fastmcp version.
"""

from __future__ import annotations

import hashlib
import json
import re
from base64 import urlsafe_b64encode
from typing import Any

import httpx
import pytest
from fastmcp.server.auth import cimd as cimd_module
from fastmcp.server.auth.ssrf import SSRFFetchError, SSRFFetchResponse

from slack_guest_reader.app import create_app
from slack_guest_reader.config import Settings
from slack_guest_reader.slack_client import SlackClient
from slack_guest_reader.store import InMemoryStore

CIMD_CLIENT_ID = "https://example.com/oauth/client-metadata"
CALLBACK = "https://example.com/oauth/callback"
CIMD_DOCUMENT: dict[str, Any] = {
    "client_id": CIMD_CLIENT_ID,
    "client_name": "Example MCP client",
    "client_uri": "https://example.com",
    "redirect_uris": [CALLBACK],
    "grant_types": ["authorization_code", "refresh_token"],
    "response_types": ["code"],
    "token_endpoint_auth_method": "none",
}


def pkce_params() -> dict[str, str]:
    verifier = "test-verifier-" + "x" * 40
    challenge = urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    return {"code_challenge": challenge.decode(), "code_challenge_method": "S256"}


def authorize_params(
    client_id: str, redirect_uri: str = CALLBACK, scope: str | None = None
) -> dict[str, str]:
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": "opaque-state",
        **pkce_params(),
    }
    if scope is not None:
        params["scope"] = scope
    return params


async def advertised_scopes(client: httpx.AsyncClient) -> str:
    """What a well-behaved MCP client requests: the scopes the AS metadata advertises."""
    metadata = (await client.get("/.well-known/oauth-authorization-server")).json()
    return " ".join(metadata["scopes_supported"])


@pytest.fixture
def cimd_server(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Serve CIMD documents in place of FastMCP's SSRF-safe fetch (httpx2, not respx-able).

    Returns a dict the test can edit: url -> document (or an Exception to raise).
    Also records every fetched URL in `calls`.
    """
    docs: dict[str, Any] = {CIMD_CLIENT_ID: CIMD_DOCUMENT, "calls": []}

    async def fake_fetch(url: str, **_: Any) -> SSRFFetchResponse:
        docs["calls"].append(url)
        doc = docs.get(url)
        if doc is None:
            raise SSRFFetchError(f"HTTP 404 for {url}")
        if isinstance(doc, Exception):
            raise doc
        return SSRFFetchResponse(
            content=json.dumps(doc).encode(),
            status_code=200,
            headers={"content-type": "application/json", "cache-control": "no-store"},
        )

    monkeypatch.setattr(cimd_module, "ssrf_safe_fetch_response", fake_fetch)
    return docs


@pytest.fixture
async def client(settings: Settings, slack: SlackClient) -> Any:
    app = create_app(settings, store=InMemoryStore(), slack=slack, enable_auth=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.public_base_url,
        follow_redirects=False,
    ) as http:
        yield http


def assert_not_server_error(response: httpx.Response) -> None:
    assert response.status_code < 500, (response.status_code, response.text[:500])


@pytest.mark.parametrize("with_scope", [False, True], ids=["no-scope", "advertised-scopes"])
async def test_authorize_with_cimd_client_id_resolves_the_client(
    client: httpx.AsyncClient, cimd_server: dict[str, Any], with_scope: bool
) -> None:
    scope = await advertised_scopes(client) if with_scope else None
    response = await client.get("/authorize", params=authorize_params(CIMD_CLIENT_ID, scope=scope))
    assert_not_server_error(response)
    assert cimd_server["calls"] == [CIMD_CLIENT_ID]

    # First visit: the proxy redirects to its consent page for this transaction.
    assert response.status_code in (302, 303, 307), (response.status_code, response.text[:300])
    consent_url = httpx.URL(response.headers["location"])
    assert consent_url.path == "/consent", consent_url
    assert "txn_id" in consent_url.params

    page = await client.get(consent_url)
    assert page.status_code == 200
    assert "text/html" in page.headers["content-type"]
    assert "Example MCP client" in page.text  # client_name from the CIMD document
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert csrf is not None

    # Approving hands the browser to Google with our upstream client_id.
    approved = await client.post(
        consent_url,
        data={
            "txn_id": consent_url.params["txn_id"],
            "csrf_token": csrf.group(1),
            "submit": "true",
            "action": "approve",
        },
        headers={"sec-fetch-site": "same-origin"},
    )
    assert approved.status_code in (302, 303, 307), (approved.status_code, approved.text[:300])
    google = httpx.URL(approved.headers["location"])
    assert google.host == "accounts.google.com", google
    assert google.params["client_id"] == "000000000000-test.apps.googleusercontent.com"
    assert google.params["redirect_uri"] == "https://mcp.example.com/auth/callback"
    assert google.params["code_challenge_method"] == "S256"


async def test_authorize_with_unregistered_scope_is_an_oauth_error_redirect(
    client: httpx.AsyncClient, cimd_server: dict[str, Any]
) -> None:
    """A CIMD document without `scope` gets the proxy's default (Google URIs), so a
    request for the short form `email` is an OAuth error returned to the client."""
    params = authorize_params(CIMD_CLIENT_ID, scope="openid email")
    response = await client.get("/authorize", params=params)
    assert response.status_code in (302, 303, 307), (response.status_code, response.text[:300])
    location = httpx.URL(response.headers["location"])
    assert location.copy_with(query=None) == httpx.URL(CALLBACK)
    assert location.params["error"] == "invalid_scope"
    assert location.params["state"] == "opaque-state"


async def test_authorize_with_unknown_cimd_url_is_a_client_error(
    client: httpx.AsyncClient, cimd_server: dict[str, Any]
) -> None:
    unknown = "https://example.com/oauth/does-not-exist"
    response = await client.get("/authorize", params=authorize_params(unknown))
    assert 400 <= response.status_code < 500, (response.status_code, response.text[:500])
    assert cimd_server["calls"] == [unknown]
    assert "invalid_client" in response.text or "client" in response.text.lower()


async def test_authorize_with_cimd_doc_that_fails_validation_is_a_client_error(
    client: httpx.AsyncClient, cimd_server: dict[str, Any]
) -> None:
    # The document claims a different client_id than the URL it was fetched from.
    cimd_server[CIMD_CLIENT_ID] = {**CIMD_DOCUMENT, "client_id": "https://example.org/other"}
    response = await client.get("/authorize", params=authorize_params(CIMD_CLIENT_ID))
    assert 400 <= response.status_code < 500, (response.status_code, response.text[:500])


async def test_authorize_with_cimd_client_and_foreign_redirect_uri_is_rejected(
    client: httpx.AsyncClient, cimd_server: dict[str, Any]
) -> None:
    params = authorize_params(CIMD_CLIENT_ID, redirect_uri="https://attacker.example/cb")
    response = await client.get("/authorize", params=params)
    assert 400 <= response.status_code < 500, (response.status_code, response.text[:500])
    assert "location" not in response.headers


async def test_authorize_with_random_unknown_client_id_is_a_client_error(
    client: httpx.AsyncClient, cimd_server: dict[str, Any]
) -> None:
    response = await client.get("/authorize", params=authorize_params("no-such-client"))
    assert 400 <= response.status_code < 500, (response.status_code, response.text[:500])
    assert cimd_server["calls"] == []  # not a URL, so no CIMD fetch


async def test_oauth_metadata_and_protected_resource_are_served(client: httpx.AsyncClient) -> None:
    pr = await client.get("/.well-known/oauth-protected-resource/mcp")
    assert pr.status_code == 200
    assert pr.json()["resource"].rstrip("/") == "https://mcp.example.com/mcp"

    asm = await client.get("/.well-known/oauth-authorization-server")
    assert asm.status_code == 200
    body = asm.json()
    assert body["authorization_endpoint"] == "https://mcp.example.com/authorize"
    assert "S256" in body["code_challenge_methods_supported"]

    unauth = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert unauth.status_code == 401
    assert "resource_metadata" in unauth.headers["www-authenticate"]
