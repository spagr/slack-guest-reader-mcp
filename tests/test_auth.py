import base64
import json
from typing import Any

import pytest
from mcp.server.auth.provider import TokenError

from slack_guest_reader.auth import (
    AllowlistedGoogleProvider,
    build_auth,
    build_client_storage,
    decode_jwt_payload,
    derive_fernet_key,
    is_email_allowed,
)
from slack_guest_reader.config import Settings

ALLOWED = ["operator@example.com"]


def jwt_with(payload: dict[str, Any]) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJSUzI1NiJ9.{body}.sig"


@pytest.mark.parametrize(
    ("claims", "expected"),
    [
        ({"email": "operator@example.com", "email_verified": True}, True),
        ({"email": "Operator@Example.com", "email_verified": "true"}, True),
        ({"email": "operator@example.com", "email_verified": False}, False),
        ({"email": "operator@example.com"}, False),
        ({"email": "intruder@example.com", "email_verified": True}, False),
        ({}, False),
        (None, False),
    ],
)
def test_is_email_allowed(claims: dict[str, Any] | None, expected: bool) -> None:
    assert is_email_allowed(claims, ALLOWED) is expected


def test_decode_jwt_payload() -> None:
    assert decode_jwt_payload(jwt_with({"sub": "1", "email": "a@example.com"})) == {
        "sub": "1",
        "email": "a@example.com",
    }
    assert decode_jwt_payload("garbage") is None


def test_derive_fernet_key_is_deterministic_and_valid() -> None:
    from cryptography.fernet import Fernet

    key = derive_fernet_key("GOCSPX-REPLACE-ME", salt="x")
    assert key == derive_fernet_key("GOCSPX-REPLACE-ME", salt="x")
    assert key != derive_fernet_key("GOCSPX-REPLACE-ME", salt="y")
    assert Fernet(key).decrypt(Fernet(key).encrypt(b"ok")) == b"ok"


@pytest.fixture
def provider(settings: Settings) -> AllowlistedGoogleProvider:
    return build_auth(settings, build_client_storage(settings))


async def test_code_exchange_rejects_non_allowlisted_account(
    provider: AllowlistedGoogleProvider,
) -> None:
    tokens = {
        "access_token": "ya29.x",
        "id_token": jwt_with({"sub": "9", "email": "intruder@example.com", "email_verified": True}),
    }
    with pytest.raises(TokenError) as exc:
        await provider._extract_upstream_claims(tokens)
    assert exc.value.error == "invalid_grant"


async def test_code_exchange_embeds_allowlisted_identity(
    provider: AllowlistedGoogleProvider,
) -> None:
    tokens = {
        "access_token": "ya29.x",
        "id_token": jwt_with({"sub": "1", "email": "Operator@example.com", "email_verified": True}),
    }
    claims = await provider._extract_upstream_claims(tokens)
    assert claims == {"sub": "1", "email": "operator@example.com", "email_verified": True}


async def test_code_exchange_fails_closed_without_identity(
    provider: AllowlistedGoogleProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def no_identity(_: dict[str, Any]) -> None:
        return None

    monkeypatch.setattr(provider, "_identity_claims", no_identity)
    with pytest.raises(TokenError):
        await provider._extract_upstream_claims({"access_token": "ya29.x"})


async def test_every_request_rechecks_allowlist(
    provider: AllowlistedGoogleProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastmcp.server.auth.auth import AccessToken

    def token_for(email: str) -> AccessToken:
        return AccessToken(
            token="t",
            client_id="c",
            scopes=["openid"],
            claims={"email": email, "email_verified": True},
        )

    async def allowed(self: Any, token: str) -> AccessToken:
        return token_for("operator@example.com")

    async def denied(self: Any, token: str) -> AccessToken:
        return token_for("intruder@example.com")

    monkeypatch.setattr(AllowlistedGoogleProvider.__mro__[1], "load_access_token", allowed)
    assert await provider.load_access_token("t") is not None
    monkeypatch.setattr(AllowlistedGoogleProvider.__mro__[1], "load_access_token", denied)
    assert await provider.load_access_token("t") is None


def test_oauth_metadata_points_at_public_url(provider: AllowlistedGoogleProvider) -> None:
    assert str(provider.base_url).rstrip("/") == "https://mcp.example.com"
    assert "openid" in (provider.required_scopes or [])
