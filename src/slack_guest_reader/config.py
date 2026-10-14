"""Runtime configuration, validated at startup with pydantic-settings.

Every deployment-specific value comes from environment variables (Cloud Run
injects Secret Manager values as env vars). Nothing here is hard-coded.
"""

from __future__ import annotations

import sys
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BeforeValidator, Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _split_csv(value: object) -> object:
    """Accept comma-separated strings for list fields."""
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    return value


# NoDecode stops pydantic-settings from JSON-decoding the env value before we split it.
CsvList = Annotated[list[str], NoDecode, BeforeValidator(_split_csv)]


class Settings(BaseSettings):
    """All configuration for the server. See README for the meaning of each variable."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Slack ---
    slack_bot_token: SecretStr = Field(description="Bot token (xoxb-...) from Secret Manager")
    slack_signing_secret: SecretStr | None = Field(
        default=None, description="Needed only for the /slack/events receiver (phase 2)"
    )
    slack_channel_allowlist: CsvList = Field(description="Comma-separated channel IDs")
    me_user_id: str = Field(description="Operator's Slack user ID in the target workspace")

    # --- Google OAuth (MCP side) ---
    google_client_id: str
    google_client_secret: SecretStr
    allowed_emails: CsvList = Field(description="Comma-separated Google account emails")
    public_base_url: str = Field(description="Public HTTPS URL of this service, no trailing slash")
    jwt_signing_key: SecretStr | None = Field(
        default=None,
        description="Optional explicit key for FastMCP-issued JWTs; derived from the Google "
        "client secret when unset",
    )
    storage_encryption_key: SecretStr | None = Field(
        default=None,
        description="Optional explicit key for encrypting OAuth tokens at rest; derived from "
        "the Google client secret when unset",
    )

    # --- Storage ---
    firestore_database: str = "(default)"
    firestore_enabled: bool = Field(
        default=True, description="Set false to use the in-memory store (local dev only)"
    )
    google_cloud_project: str | None = Field(
        default=None, description="GCP project for Firestore; inferred from ADC when unset"
    )

    # --- Behaviour ---
    timezone: str = "UTC"
    thread_lookback_days: int = Field(default=14, ge=1, le=365)
    notify_thread_activity: bool = True
    notify_group_mentions: bool = False
    events_ttl_days: int = Field(default=30, ge=1, le=365)
    alert_webhook_url: str | None = None
    log_level: str = "INFO"

    # --- Observability (optional) ---
    sentry_dsn: SecretStr | None = Field(
        default=None, description="Sentry DSN from Secret Manager; unset disables Sentry"
    )
    sentry_environment: str = "production"
    sentry_traces_sample_rate: float = Field(default=0.0, ge=0.0, le=1.0)

    # --- Server ---
    host: str = "0.0.0.0"  # noqa: S104 - Cloud Run requires binding all interfaces
    port: int = 8080

    @field_validator("slack_bot_token")
    @classmethod
    def _bot_token_prefix(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith("xoxb-"):
            raise ValueError("SLACK_BOT_TOKEN must be a bot token starting with 'xoxb-'")
        return value

    @field_validator("slack_channel_allowlist")
    @classmethod
    def _channels_non_empty(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("SLACK_CHANNEL_ALLOWLIST must contain at least one channel ID")
        for channel in value:
            if not (channel[:1] in {"C", "G"} and channel[1:].isalnum()):
                raise ValueError(f"'{channel}' does not look like a Slack channel ID (C…/G…)")
        return value

    @field_validator("me_user_id")
    @classmethod
    def _user_id_shape(cls, value: str) -> str:
        if not (value[:1] in {"U", "W"} and value[1:].isalnum()):
            raise ValueError("ME_USER_ID must be a Slack user ID (U…/W…)")
        return value

    @field_validator("allowed_emails")
    @classmethod
    def _emails(cls, value: list[str]) -> list[str]:
        emails = [email.lower() for email in value]
        if not emails:
            raise ValueError("ALLOWED_EMAILS must contain at least one email")
        for email in emails:
            if "@" not in email:
                raise ValueError(f"'{email}' is not an email address")
        return emails

    @field_validator("public_base_url", "alert_webhook_url")
    @classmethod
    def _https_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.rstrip("/")
        if not (value.startswith("https://") or value.startswith("http://localhost")):
            raise ValueError("expected an https:// URL (http://localhost is allowed for dev)")
        return value

    @field_validator("timezone")
    @classmethod
    def _tz(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown TIMEZONE '{value}'") from exc
        return value

    @field_validator("log_level")
    @classmethod
    def _level(cls, value: str) -> str:
        value = value.upper()
        if value not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING or ERROR")
        return value

    @property
    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def channel_set(self) -> frozenset[str]:
        return frozenset(self.slack_channel_allowlist)


def load_settings() -> Settings:
    """Load settings or exit with a readable list of problems."""
    try:
        return Settings()  # type: ignore[call-arg]  # values come from the environment
    except ValidationError as exc:
        lines = ["Configuration error:"]
        for err in exc.errors():
            name = ".".join(str(part) for part in err["loc"]).upper() or "<settings>"
            lines.append(f"  {name}: {err['msg']}")
        print("\n".join(lines), file=sys.stderr)
        raise SystemExit(1) from None
