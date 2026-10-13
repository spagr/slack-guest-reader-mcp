from zoneinfo import ZoneInfo

from slack_guest_reader.render import (
    format_message,
    has_group_mention,
    mentions_user,
    permalink,
    render_text,
    snippet,
    ts_to_iso,
    user_ids_in,
)

NAMES = {"U0000000001": "Me Operator", "U0000000002": "Alice"}


def test_render_text_resolves_entities() -> None:
    text = (
        "hey <@U0000000002> and <@U0000000009|fallback>, see <#C0000000001|project-alpha> "
        "and <https://example.com/x|the doc> or <https://example.com/y> &amp; <!here>"
    )
    assert render_text(text, NAMES) == (
        "hey @Alice and @fallback, see #project-alpha and the doc (https://example.com/x) "
        "or https://example.com/y & @here"
    )


def test_mentions_and_group_mentions() -> None:
    assert mentions_user("ping <@U0000000001>", "U0000000001")
    assert not mentions_user("ping <@U0000000001>", "U0000000002")
    assert user_ids_in("<@U0000000001> <@U0000000002|x>") == {"U0000000001", "U0000000002"}
    assert has_group_mention("<!channel> all")
    assert not has_group_mention("no mentions")


def test_ts_and_permalink() -> None:
    assert ts_to_iso("1700000000.000100", ZoneInfo("UTC")) == "2023-11-14T22:13:20+00:00"
    assert permalink("team", "C0000000001", "1700000000.000100") == (
        "https://team.slack.com/archives/C0000000001/p1700000000000100"
    )
    assert permalink("team", "C0000000001", "1700000001.0001", "1700000000.000100") == (
        "https://team.slack.com/archives/C0000000001/p17000000010001"
        "?thread_ts=1700000000.000100&cid=C0000000001"
    )
    assert permalink(None, "C0000000001", "1") is None


def test_format_message_is_compact_and_metadata_only() -> None:
    raw = {
        "ts": "1700000000.000100",
        "user": "U0000000002",
        "text": "hi <@U0000000001>",
        "reply_count": 2,
        "reactions": [{"name": "eyes", "count": 2, "users": ["U0000000001", "U0000000002"]}],
        "files": [
            {
                "id": "F1",
                "name": "spec.pdf",
                "filetype": "pdf",
                "permalink": "https://x/f",
                "url_private_download": "https://should-not-appear",
            }
        ],
        "edited": {"user": "U0000000002"},
    }
    out = format_message(
        raw,
        channel="C0000000001",
        names=NAMES,
        tz=ZoneInfo("UTC"),
        me_user_id="U0000000001",
        team_domain="team",
    )
    assert out["author"] == {"id": "U0000000002", "name": "Alice"}
    assert out["text"] == "hi @Me Operator"
    assert out["reactions"] == [{"name": "eyes", "count": 2, "me": True}]
    assert out["files"] == [
        {"id": "F1", "name": "spec.pdf", "type": "pdf", "permalink": "https://x/f"}
    ]
    assert out["edited"] is True
    assert "url_private_download" not in str(out)
    assert out["permalink"].endswith("/p1700000000000100")


def test_snippet() -> None:
    assert snippet("a   b\nc") == "a b c"
    assert snippet("x" * 200, 10) == "xxxxxxxxx…"
