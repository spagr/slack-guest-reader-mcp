"""Sentry is optional and must never be configured to leak message content."""

from __future__ import annotations

import warnings

import sentry_sdk
from pydantic import SecretStr

from slack_guest_reader.config import Settings
from slack_guest_reader.observability import init_sentry

PLACEHOLDER_DSN = "https://examplePublicKey@o0.ingest.sentry.io/0"


def test_sentry_disabled_without_dsn(settings: Settings) -> None:
    assert settings.sentry_dsn is None
    assert init_sentry(settings) is False
    assert not sentry_sdk.get_client().is_active()


def test_sentry_enabled_with_privacy_defaults(settings: Settings) -> None:
    enabled = settings.model_copy(update={"sentry_dsn": SecretStr(PLACEHOLDER_DSN)})
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")  # no deprecated options
            assert init_sentry(enabled) is True
        options = sentry_sdk.get_client().options
        collection = options["data_collection"]
        assert collection["user_info"] is False
        assert collection["http_bodies"] == []
        assert collection["stack_frame_variables"] is False
        assert collection["url_query_params"]["mode"] == "off"
        assert options["traces_sample_rate"] == 0.0
        assert options["environment"] == "production"
        assert options["release"].startswith("slack-guest-reader@")
    finally:
        sentry_sdk.get_client().close(timeout=0)
