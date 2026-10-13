"""Parse `since` / `until` arguments: ISO 8601 or relative values like "3d", "12h"."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

RELATIVE_RE = re.compile(r"^\s*(\d+)\s*([mhdw])\s*$", re.IGNORECASE)
_UNITS = {"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


def parse_time(value: str, *, tz: ZoneInfo, now: datetime | None = None) -> datetime:
    """Return an aware datetime. Naive ISO values are interpreted in `tz`."""
    now = now or datetime.now(UTC)
    match = RELATIVE_RE.match(value)
    if match:
        amount, unit = int(match.group(1)), match.group(2).lower()
        return now - timedelta(**{_UNITS[unit]: amount})
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(
            f"Cannot parse time '{value}'. Use ISO 8601 (2025-01-31 or 2025-01-31T09:00:00+01:00) "
            "or a relative value like '3d', '12h', '30m', '2w'."
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed


def to_slack_ts(moment: datetime) -> str:
    """Slack `ts` strings are seconds since the epoch with 6 decimals."""
    return f"{moment.timestamp():.6f}"


def ts_to_datetime(ts: str) -> datetime:
    return datetime.fromtimestamp(float(ts), tz=UTC)
