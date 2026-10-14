"""Optional Sentry error reporting.

Privacy first: this server must never ship message text, user names or tokens
anywhere, so Sentry gets exceptions and request metadata only. No user info, no
request bodies, no query strings, no local variables in stack frames (they could
hold Slack payloads). The settings are fixed here on purpose, not configurable.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from . import __version__
from .config import Settings

if TYPE_CHECKING:  # the TypedDict only exists for type checkers in sentry-sdk
    from sentry_sdk._types import DataCollectionUserOptions

logger = logging.getLogger(__name__)

# sentry-sdk 2.x `data_collection` (replaces the deprecated send_default_pii).
DATA_COLLECTION: DataCollectionUserOptions = {
    "user_info": False,
    "cookies": {"mode": "off"},
    "http_bodies": [],
    "url_query_params": {"mode": "off"},
    "graphql": {"document": False, "variables": False},
    "gen_ai": {"inputs": False, "outputs": False},
    "database_query_data": False,
    "queues": False,
    "stack_frame_variables": False,
}


def init_sentry(settings: Settings) -> bool:
    """Initialise Sentry when SENTRY_DSN is set. Returns True when active."""
    if settings.sentry_dsn is None:
        return False
    import sentry_sdk
    from sentry_sdk.integrations.starlette import StarletteIntegration

    sentry_sdk.init(
        dsn=settings.sentry_dsn.get_secret_value(),
        release=f"slack-guest-reader@{__version__}",
        environment=settings.sentry_environment,
        traces_sample_rate=settings.sentry_traces_sample_rate,
        data_collection=DATA_COLLECTION,
        integrations=[StarletteIntegration(transaction_style="endpoint")],
    )
    logger.info("sentry enabled environment=%s", settings.sentry_environment)
    return True
