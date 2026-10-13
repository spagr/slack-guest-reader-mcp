"""MCP-side authentication: Google sign-in through FastMCP's OAuth proxy, plus an
email allowlist enforced both when tokens are issued and on every request.

Why a proxy: Claude's custom connectors expect the MCP server itself to be an
OAuth authorization server (metadata discovery, dynamic client registration,
PKCE). Google does not offer dynamic client registration, so FastMCP's
`GoogleProvider` fronts Google with MCP-compliant endpoints and issues its own
JWTs that reference the upstream Google token.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Iterable
from typing import Any

import httpx
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.providers.google import GoogleProvider
from key_value.aio.protocols import AsyncKeyValue
from key_value.aio.stores.memory import MemoryStore
from key_value.aio.wrappers.encryption import FernetEncryptionWrapper
from mcp.server.auth.provider import TokenError

from .config import Settings

logger = logging.getLogger(__name__)

GOOGLE_TOKENINFO = "https://oauth2.googleapis.com/tokeninfo"
REQUIRED_SCOPES = ["openid", "email"]


def is_email_allowed(claims: dict[str, Any] | None, allowed: Iterable[str]) -> bool:
    """True only for a verified email that is on the allowlist (case-insensitive)."""
    if not claims:
        return False
    email = str(claims.get("email") or "").strip().lower()
    verified = claims.get("email_verified")
    if isinstance(verified, str):
        verified = verified.lower() == "true"
    if not email or not verified:
        return False
    return email in {e.lower() for e in allowed}


def decode_jwt_payload(token: str) -> dict[str, Any] | None:
    """Decode the payload of a JWT without verifying it.

    Only used for the `id_token` Google returns directly from its token endpoint
    over TLS in exchange for our client secret, where OpenID Connect Core 3.1.3.7
    permits skipping signature validation.
    """
    try:
        payload = token.split(".")[1]
        padded = payload + "=" * (-len(payload) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded))
    except (IndexError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class AllowlistedGoogleProvider(GoogleProvider):
    """GoogleProvider that only lets allowlisted, verified Google accounts in."""

    def __init__(self, *, allowed_emails: Iterable[str], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._allowed_emails = frozenset(e.lower() for e in allowed_emails)

    async def _identity_claims(self, idp_tokens: dict[str, Any]) -> dict[str, Any] | None:
        id_token = idp_tokens.get("id_token")
        if id_token:
            claims = decode_jwt_payload(str(id_token))
            if claims:
                return claims
        access_token = idp_tokens.get("access_token")
        if not access_token:
            return None
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(GOOGLE_TOKENINFO, params={"access_token": access_token})
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        data = response.json()
        return data if isinstance(data, dict) else None

    async def _extract_upstream_claims(self, idp_tokens: dict[str, Any]) -> dict[str, Any] | None:
        """Called when exchanging the Google code (and on refresh): reject early."""
        claims = await self._identity_claims(idp_tokens)
        if not is_email_allowed(claims, self._allowed_emails):
            logger.warning("rejected sign-in: account not on the allowlist or email unverified")
            raise TokenError(
                "invalid_grant", "This Google account is not allowed to use this server."
            )
        assert claims is not None
        return {
            "sub": claims.get("sub"),
            "email": str(claims.get("email")).lower(),
            "email_verified": True,
        }

    async def load_access_token(self, token: str) -> AccessToken | None:
        """Called on every MCP request: defence in depth against allowlist edits."""
        access = await super().load_access_token(token)
        if access is None:
            return None
        if not isinstance(access, AccessToken):  # pragma: no cover - SDK base type
            access = AccessToken(**access.model_dump())
        claims = dict(access.claims or {})
        upstream = claims.get("upstream_claims") or {}
        candidate = {
            "email": claims.get("email") or upstream.get("email"),
            "email_verified": claims.get("email_verified")
            if claims.get("email_verified") is not None
            else upstream.get("email_verified"),
        }
        if not is_email_allowed(candidate, self._allowed_emails):
            logger.warning("rejected request: token belongs to a non-allowlisted account")
            return None
        return access


def derive_fernet_key(material: str, *, salt: str) -> bytes:
    """HKDF-SHA256 → 32 bytes → urlsafe base64, the format Fernet expects."""
    derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt.encode(), info=b"Fernet").derive(
        material.encode()
    )
    return base64.urlsafe_b64encode(derived)


def build_client_storage(settings: Settings, backend: AsyncKeyValue | None = None) -> AsyncKeyValue:
    """Encrypt OAuth client registrations and upstream tokens at rest."""
    material = (
        settings.storage_encryption_key.get_secret_value()
        if settings.storage_encryption_key
        else settings.google_client_secret.get_secret_value()
    )
    fernet = Fernet(derive_fernet_key(material, salt="slack-guest-reader/oauth-storage"))
    return FernetEncryptionWrapper(key_value=backend or MemoryStore(), fernet=fernet)


def build_auth(settings: Settings, client_storage: AsyncKeyValue) -> AllowlistedGoogleProvider:
    return AllowlistedGoogleProvider(
        allowed_emails=settings.allowed_emails,
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret.get_secret_value(),
        base_url=settings.public_base_url,
        required_scopes=REQUIRED_SCOPES,
        client_storage=client_storage,
        jwt_signing_key=(
            settings.jwt_signing_key.get_secret_value() if settings.jwt_signing_key else None
        ),
        cache_ttl_seconds=300,
        require_authorization_consent="remember",
        extra_authorize_params={"prompt": "select_account consent"},
    )
