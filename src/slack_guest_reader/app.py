"""FastMCP server, HTTP routes and startup."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.auth import AuthProvider
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from . import __version__
from .auth import build_auth, build_client_storage
from .config import Settings, load_settings
from .events import EventProcessor, make_events_endpoint
from .slack_client import SlackClient
from .store import FirestoreStore, InMemoryStore, Store
from .tools import Services, register_tools

logger = logging.getLogger(__name__)

INSTRUCTIONS = """\
Read-only access to an allowlisted set of Slack channels in a workspace the operator
is a guest in. Start with list_channels. Use get_messages for recent channel history,
get_thread to expand a thread, search_messages for keyword search over recent history,
and get_notifications for "what's new for me" (mentions, replies, thread activity,
reactions). Nothing here can post, react or modify anything in Slack.
"""


def build_store(settings: Settings) -> Store:
    if not settings.firestore_enabled:
        logger.warning(
            "FIRESTORE_ENABLED=false: using the in-memory store (state is not persisted)"
        )
        return InMemoryStore()
    from google.cloud import firestore

    client = firestore.AsyncClient(
        project=settings.google_cloud_project, database=settings.firestore_database
    )
    return FirestoreStore(client)


def build_oauth_backend(settings: Settings) -> Any:
    """Key-value backend for FastMCP's OAuth state (clients, tokens, transactions)."""
    if not settings.firestore_enabled:
        return None
    from google.cloud import firestore
    from key_value.aio.stores.firestore import FirestoreStore as KVFirestoreStore

    client = firestore.AsyncClient(
        project=settings.google_cloud_project, database=settings.firestore_database
    )
    return KVFirestoreStore(client=client, default_collection="oauth")


def create_mcp(
    settings: Settings,
    *,
    store: Store,
    slack: SlackClient,
    auth: AuthProvider | None,
) -> tuple[FastMCP, Services]:
    services = Services.build(settings, slack, store)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastMCP) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await slack.aclose()

    mcp = FastMCP(
        "slack-guest-reader",
        instructions=INSTRUCTIONS,
        version=__version__,
        auth=auth,
        lifespan=lifespan,
        mask_error_details=True,
    )
    register_tools(mcp, services)

    @mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
    async def healthz(_: Request) -> Response:
        return JSONResponse({"status": "ok", "version": __version__})

    if settings.slack_signing_secret is not None:
        processor = EventProcessor(
            services,
            ttl_seconds=settings.events_ttl_days * 86400,
            alert_webhook_url=settings.alert_webhook_url,
        )
        endpoint = make_events_endpoint(settings.slack_signing_secret.get_secret_value(), processor)
        mcp.custom_route("/slack/events", methods=["POST"], include_in_schema=False)(endpoint)
        services.event_processor = processor
    return mcp, services


def create_app(
    settings: Settings,
    *,
    store: Store | None = None,
    slack: SlackClient | None = None,
    auth: AuthProvider | None = None,
    enable_auth: bool = True,
) -> Any:
    """Build the ASGI app. Tests pass fakes and `enable_auth=False`."""
    store = store or build_store(settings)
    slack = slack or SlackClient(settings.slack_bot_token.get_secret_value())
    if auth is None and enable_auth:
        auth = build_auth(settings, build_client_storage(settings, build_oauth_backend(settings)))
    mcp, _ = create_mcp(settings, store=store, slack=slack, auth=auth)
    return mcp.http_app(path="/mcp", stateless_http=True)


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main() -> None:
    settings = load_settings()
    configure_logging(settings.log_level)
    import uvicorn

    app = create_app(settings)
    logger.info(
        "starting slack-guest-reader %s channels=%d events=%s",
        __version__,
        len(settings.slack_channel_allowlist),
        settings.slack_signing_secret is not None,
    )
    uvicorn.run(app, host=settings.host, port=settings.port, log_level=settings.log_level.lower())


if __name__ == "__main__":
    main()
