import pytest
from pydantic import ValidationError
from tests.conftest import make_settings


def test_csv_lists_and_lowercase_emails() -> None:
    s = make_settings(ALLOWED_EMAILS=" A@Example.com, b@example.com ")
    assert s.allowed_emails == ["a@example.com", "b@example.com"]
    assert s.slack_channel_allowlist == ["C0000000001", "G0000000002"]
    assert s.tzinfo.key == "Europe/Prague"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("SLACK_BOT_TOKEN", "xoxp-TEST-TOKEN"),
        ("SLACK_CHANNEL_ALLOWLIST", ""),
        ("SLACK_CHANNEL_ALLOWLIST", "general"),
        ("ME_USER_ID", "nobody"),
        ("ALLOWED_EMAILS", "not-an-email"),
        ("PUBLIC_BASE_URL", "http://insecure.example.com"),
        ("TIMEZONE", "Mars/Olympus"),
        ("LOG_LEVEL", "LOUD"),
    ],
)
def test_invalid_values_fail_fast(key: str, value: str) -> None:
    with pytest.raises(ValidationError):
        make_settings(**{key: value})


def test_public_url_trailing_slash_stripped() -> None:
    assert make_settings(PUBLIC_BASE_URL="https://x.example.com/").public_base_url == (
        "https://x.example.com"
    )


def test_load_settings_exits_with_readable_error(monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # type: ignore[no-untyped-def]
    from slack_guest_reader.config import load_settings

    for key in list(__import__("os").environ):
        if key.startswith(("SLACK_", "GOOGLE_", "ALLOWED_", "PUBLIC_", "ME_")):
            monkeypatch.delenv(key)
    monkeypatch.chdir("/")  # no .env here
    with pytest.raises(SystemExit) as exc:
        load_settings()
    assert exc.value.code == 1
    assert "SLACK_BOT_TOKEN" in capsys.readouterr().err


def test_settings_load_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.conftest import BASE_ENV

    from slack_guest_reader.config import Settings

    for key, value in BASE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("ALLOWED_EMAILS", "a@example.com, B@example.com")
    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.slack_channel_allowlist == ["C0000000001", "G0000000002"]
    assert s.allowed_emails == ["a@example.com", "b@example.com"]
