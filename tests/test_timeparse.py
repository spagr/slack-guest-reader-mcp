from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from slack_guest_reader.timeparse import parse_time, to_slack_ts

TZ = ZoneInfo("Europe/Prague")
NOW = datetime(2025, 6, 1, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("3d", datetime(2025, 5, 29, 12, 0, tzinfo=UTC)),
        ("12h", datetime(2025, 6, 1, 0, 0, tzinfo=UTC)),
        ("30m", datetime(2025, 6, 1, 11, 30, tzinfo=UTC)),
        ("1w", datetime(2025, 5, 25, 12, 0, tzinfo=UTC)),
        ("2025-05-01", datetime(2025, 5, 1, tzinfo=TZ)),
        ("2025-05-01T10:00:00Z", datetime(2025, 5, 1, 10, 0, tzinfo=UTC)),
        ("2025-05-01T10:00:00+02:00", datetime(2025, 5, 1, 8, 0, tzinfo=UTC)),
    ],
)
def test_parse_time(value: str, expected: datetime) -> None:
    assert parse_time(value, tz=TZ, now=NOW) == expected


def test_parse_time_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="Cannot parse time"):
        parse_time("yesterday-ish", tz=TZ)


def test_to_slack_ts() -> None:
    assert to_slack_ts(datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)) == "1700000000.000000"
