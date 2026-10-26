"""Phase 2: Slack Events API receiver."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import respx
from tests.conftest import ALICE, BOB, CHANNEL, ME, OTHER, FakeSlack, make_settings, msg

from slack_guest_reader.app import create_app, create_mcp
from slack_guest_reader.config import Settings
from slack_guest_reader.events import EventProcessor, verify_slack_signature
from slack_guest_reader.slack_client import SlackClient
from slack_guest_reader.store import InMemoryStore
from slack_guest_reader.tools import collect_notifications, render_notifications

SECRET = "slack-signing-secret-for-tests"


def sign(body: bytes, *, ts: int | None = None, secret: str = SECRET) -> dict[str, str]:
    ts = ts or int(time.time())
    base = b"v0:" + str(ts).encode() + b":" + body
    sig = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return {"X-Slack-Request-Timestamp": str(ts), "X-Slack-Signature": sig}


def test_verify_signature() -> None:
    body = b'{"type":"event_callback"}'
    good = sign(body)
    assert verify_slack_signature(SECRET, body=body, **_hdr(good))
    assert not verify_slack_signature("other-secret", body=body, **_hdr(good))
    assert not verify_slack_signature(SECRET, body=body + b" ", **_hdr(good))
    stale = sign(body, ts=int(time.time()) - 600)
    assert not verify_slack_signature(SECRET, body=body, **_hdr(stale))
    assert not verify_slack_signature(SECRET, body=body, timestamp=None, signature=None)
    assert not verify_slack_signature(SECRET, body=body, timestamp="nope", signature="v0=x")


def _hdr(h: dict[str, str]) -> dict[str, str]:
    return {"timestamp": h["X-Slack-Request-Timestamp"], "signature": h["X-Slack-Signature"]}


def envelope(event: dict[str, Any], event_id: str = "Ev0000000001") -> dict[str, Any]:
    return {
        "type": "event_callback",
        "event_id": event_id,
        "event_time": int(time.time()),
        "team_id": "T0000000000",
        "event": event,
    }


@contextlib.asynccontextmanager
async def running(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    """Run the app's lifespan once and yield an HTTP client bound to it."""
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            yield http


async def post(
    http: httpx.AsyncClient, payload: dict[str, Any], headers: dict[str, str] | None = None
) -> httpx.Response:
    body = json.dumps(payload).encode()
    headers = sign(body) if headers is None else headers
    return await http.post(
        "/slack/events", content=body, headers={**headers, "Content-Type": "application/json"}
    )


def events_settings(**overrides: str) -> Settings:
    return make_settings(SLACK_SIGNING_SECRET=SECRET, **overrides)


async def test_endpoint_absent_without_signing_secret(
    settings: Settings, slack: SlackClient
) -> None:
    app = create_app(settings, store=InMemoryStore(), slack=slack, enable_auth=False)
    async with running(app) as http:
        response = await post(http, {"type": "url_verification", "challenge": "x"})
    assert response.status_code == 404


async def test_url_verification_and_bad_signature(
    fake_slack: FakeSlack, slack: SlackClient
) -> None:
    app = create_app(events_settings(), store=InMemoryStore(), slack=slack, enable_auth=False)
    async with running(app) as http:
        ok = await post(http, {"type": "url_verification", "challenge": "abc123"})
        assert ok.status_code == 200 and ok.json() == {"challenge": "abc123"}
        bad = await post(http, {"type": "url_verification", "challenge": "abc123"}, headers={})
        assert bad.status_code == 401
        forged = await post(http, {"type": "url_verification"}, headers=sign(b"x", secret="wrong"))
        assert forged.status_code == 401


async def test_message_events_are_classified_stored_and_deduplicated(
    fake_slack: FakeSlack, slack: SlackClient
) -> None:
    store = InMemoryStore()
    settings = events_settings()
    app = create_app(settings, store=store, slack=slack, enable_auth=False)
    now = time.time()
    parent_ts = f"{now - 3600:.6f}"
    fake_slack.add(CHANNEL, msg(parent_ts, ME, "my question"))

    mention = envelope(
        {
            "type": "message",
            "channel": CHANNEL,
            "user": ALICE,
            "text": f"<@{ME}> hi",
            "ts": f"{now - 100:.6f}",
        },
        "Ev1",
    )
    reply = envelope(
        {
            "type": "message",
            "channel": CHANNEL,
            "user": BOB,
            "text": "answer",
            "ts": f"{now - 50:.6f}",
            "thread_ts": parent_ts,
        },
        "Ev2",
    )
    mine = envelope(
        {"type": "message", "channel": CHANNEL, "user": ME, "text": "me", "ts": f"{now - 40:.6f}"},
        "Ev3",
    )
    other_channel = envelope(
        {
            "type": "message",
            "channel": OTHER,
            "user": ALICE,
            "text": f"<@{ME}> not allowlisted",
            "ts": f"{now - 30:.6f}",
        },
        "Ev4",
    )
    plain = envelope(
        {
            "type": "message",
            "channel": CHANNEL,
            "user": ALICE,
            "text": "fyi",
            "ts": f"{now - 20:.6f}",
        },
        "Ev5",
    )
    async with running(app) as http:
        for payload in (mention, reply, mine, other_channel, plain, mention):
            assert (await post(http, payload)).status_code == 200

    stored = await store.list_events("0", 50)
    assert [(e.event_id, e.kind) for e in stored] == [("Ev2", "reply_to_me"), ("Ev1", "mention")]
    assert stored[0].payload["parent"] == {"ts": parent_ts, "user": ME, "text": "my question"}
    meta = await store.get_meta("events")
    assert meta and meta["first_received_at"] <= time.time()


async def test_reaction_events(fake_slack: FakeSlack, slack: SlackClient) -> None:
    store = InMemoryStore()
    app = create_app(events_settings(), store=store, slack=slack, enable_auth=False)
    now = time.time()
    item_ts = f"{now - 500:.6f}"
    on_mine = envelope(
        {
            "type": "reaction_added",
            "user": ALICE,
            "reaction": "eyes",
            "item_user": ME,
            "item": {"type": "message", "channel": CHANNEL, "ts": item_ts},
            "event_ts": f"{now - 10:.6f}",
        },
        "Ev10",
    )
    on_theirs = envelope(
        {
            "type": "reaction_added",
            "user": ALICE,
            "reaction": "eyes",
            "item_user": BOB,
            "item": {"type": "message", "channel": CHANNEL, "ts": item_ts},
            "event_ts": f"{now - 9:.6f}",
        },
        "Ev11",
    )
    by_me = envelope(
        {
            "type": "reaction_added",
            "user": ME,
            "reaction": "eyes",
            "item_user": ME,
            "item": {"type": "message", "channel": CHANNEL, "ts": item_ts},
            "event_ts": f"{now - 8:.6f}",
        },
        "Ev12",
    )
    async with running(app) as http:
        for payload in (on_mine, on_theirs, by_me):
            assert (await post(http, payload)).status_code == 200
    stored = await store.list_events("0", 50)
    assert [(e.event_id, e.kind) for e in stored] == [("Ev10", "reaction")]
    assert stored[0].payload["reaction"] == "eyes"


async def test_get_notifications_reads_events_then_polls_only_the_gap(
    fake_slack: FakeSlack, slack: SlackClient
) -> None:
    store = InMemoryStore()
    settings = events_settings()
    _, services = create_mcp(settings, store=store, slack=slack, auth=None)
    processor: EventProcessor = services.event_processor
    now = datetime.now(UTC)
    old_mention_ts = f"{(now - timedelta(hours=10)).timestamp():.6f}"
    fake_slack.add(CHANNEL, msg(old_mention_ts, ALICE, f"<@{ME}> before events went live"))
    my_ts = f"{(now - timedelta(hours=9)).timestamp():.6f}"
    fake_slack.add(CHANNEL, msg(my_ts, ME, "my message"))

    # Events receiver comes online "now" and gets a reaction + a mention.
    await processor.process(
        envelope(
            {
                "type": "reaction_added",
                "user": BOB,
                "reaction": "tada",
                "item_user": ME,
                "item": {"type": "message", "channel": CHANNEL, "ts": my_ts},
                "event_ts": f"{(now + timedelta(seconds=1)).timestamp():.6f}",
            },
            "EvR",
        )
    )
    await processor.process(
        envelope(
            {
                "type": "message",
                "channel": CHANNEL,
                "user": ALICE,
                "text": f"<@{ME}> live",
                "ts": f"{(now + timedelta(seconds=2)).timestamp():.6f}",
            },
            "EvM",
        )
    )

    fake_slack.calls.clear()
    raw = await collect_notifications(
        services, since="1d", limit=50, now=now + timedelta(seconds=5)
    )
    items = await render_notifications(services, raw)
    assert [(n["type"], n["source"]) for n in items] == [
        ("mention", "event"),
        ("reaction", "event"),
        ("mention", "poll"),
    ]
    assert items[0]["message"]["text"] == "@Me Operator live"
    assert items[1]["reaction"] == {"name": "tada", "by": "Bob B"}
    assert items[1]["message"]["text"] == "my message"  # fetched on demand
    assert items[2]["message"]["text"] == "@Me Operator before events went live"
    history_calls = [c[1] for c in fake_slack.calls if c[0] == "conversations.history"]
    gap_call = next(c for c in history_calls if "latest" in c)
    assert float(gap_call["latest"]) <= now.timestamp() + 1  # polled only up to events-live time


async def test_get_notifications_polls_again_when_receiver_is_silent(
    fake_slack: FakeSlack, slack: SlackClient
) -> None:
    """Safety net: a receiver that has seen nothing for EVENTS_STALE_HOURS no longer
    counts as the only source; the window is polled as well and duplicates dropped."""
    store = InMemoryStore()
    settings = events_settings(EVENTS_STALE_HOURS="6")
    _, services = create_mcp(settings, store=store, slack=slack, auth=None)
    now = datetime.now(UTC)
    live_from = (now - timedelta(days=2)).timestamp()
    missed_ts = f"{(now - timedelta(hours=1)).timestamp():.6f}"
    fake_slack.add(CHANNEL, msg(missed_ts, ALICE, f"<@{ME}> did this arrive?"))

    # Receiver alive an hour ago: inside the 1d window only events count, no polling.
    await store.set_meta(
        "events",
        {
            "first_received_at": live_from,
            "last_received_at": (now - timedelta(hours=1)).timestamp(),
        },
    )
    fake_slack.calls.clear()
    raw = await collect_notifications(services, since="1d", limit=50, now=now)
    assert raw == []
    assert not [c for c in fake_slack.calls if c[0] == "conversations.history"]

    # Receiver silent for 7 h: the same window is polled and the missed mention shows up.
    await store.set_meta(
        "events",
        {
            "first_received_at": live_from,
            "last_received_at": (now - timedelta(hours=7)).timestamp(),
        },
    )
    fake_slack.calls.clear()
    raw = await collect_notifications(services, since="1d", limit=50, now=now)
    items = await render_notifications(services, raw)
    assert [(n["type"], n["source"]) for n in items] == [("mention", "poll")]
    assert items[0]["message"]["ts"] == missed_ts
    assert [c for c in fake_slack.calls if c[0] == "conversations.history"]


async def test_alert_webhook_posts_for_mentions(fake_slack: FakeSlack, slack: SlackClient) -> None:
    store = InMemoryStore()
    settings = events_settings(ALERT_WEBHOOK_URL="https://alerts.example.com/hook")
    _, services = create_mcp(settings, store=store, slack=slack, auth=None)
    with respx.mock(assert_all_called=True) as router:
        hook = router.post("https://alerts.example.com/hook").mock(return_value=httpx.Response(204))
        await services.event_processor.process(
            envelope(
                {
                    "type": "message",
                    "channel": CHANNEL,
                    "user": ALICE,
                    "text": f"<@{ME}> urgent",
                    "ts": "1700000000.000100",
                },
                "EvA",
            )
        )
    sent = json.loads(hook.calls[0].request.content)
    assert sent["type"] == "mention"
    assert sent["permalink"].endswith("/p1700000000000100")
    assert sent["snippet"] == f"<@{ME}> urgent"


# --- Hydration of event notifications -------------------------------------------------


def reaction_event(item_ts: str, event_id: str, *, reaction: str = "+1") -> dict[str, Any]:
    return envelope(
        {
            "type": "reaction_added",
            "user": ALICE,
            "reaction": reaction,
            "item_user": ME,
            "item": {"type": "message", "channel": CHANNEL, "ts": item_ts},
            "event_ts": f"{float(item_ts) + 60:.6f}",
        },
        event_id,
    )


async def notifications_since(services: Any, since: str = "1d") -> list[dict[str, Any]]:
    raw = await collect_notifications(services, since=since, limit=50)
    return await render_notifications(services, raw)


async def test_reaction_on_top_level_message_is_hydrated_via_history(
    fake_slack: FakeSlack, slack: SlackClient
) -> None:
    store = InMemoryStore()
    _, services = create_mcp(events_settings(), store=store, slack=slack, auth=None)
    ts = f"{time.time() - 600:.6f}"
    fake_slack.add(
        CHANNEL,
        {**msg(ts, ME, "top-level text"), "edited": {"user": ME, "ts": ts}},
    )
    await services.event_processor.process(reaction_event(ts, "EvTop"))

    fake_slack.calls.clear()
    [item] = await notifications_since(services)
    assert (item["type"], item["source"]) == ("reaction", "event")
    assert item["message"]["text"] == "top-level text"
    assert item["message"]["edited"] is True
    # Slack has not listed the reaction yet in this fake: the event's reaction is added.
    assert item["message"]["reactions"] == [{"name": "+1", "count": 1, "me": False}]
    assert "hydrated" not in item
    assert [c[0] for c in fake_slack.calls].count("conversations.replies") == 0


async def test_reaction_on_thread_reply_is_hydrated_via_replies(
    fake_slack: FakeSlack, slack: SlackClient
) -> None:
    """The real-world case: reaction_added has no thread_ts and history has no replies."""
    store = InMemoryStore()
    _, services = create_mcp(events_settings(), store=store, slack=slack, auth=None)
    now = time.time()
    parent_ts = f"{now - 3600:.6f}"
    reply_ts = f"{now - 600:.6f}"
    fake_slack.add(CHANNEL, msg(parent_ts, BOB, "the thread parent"))
    fake_slack.add_reply(
        CHANNEL,
        parent_ts,
        msg(
            reply_ts,
            ME,
            "my reply in the thread",
            reactions=[{"name": "+1", "count": 1, "users": [ALICE]}],
        ),
    )
    await services.event_processor.process(reaction_event(reply_ts, "EvReply"))

    [item] = await notifications_since(services)
    assert item["message"]["text"] == "my reply in the thread"
    assert item["message"]["thread_ts"] == parent_ts
    assert item["message"]["reactions"] == [{"name": "+1", "count": 1, "me": False}]
    assert item["reaction"] == {"name": "+1", "by": "Alice"}
    assert "hydrated" not in item


async def test_reaction_on_missing_message_is_flagged_not_hydrated(
    fake_slack: FakeSlack, slack: SlackClient
) -> None:
    store = InMemoryStore()
    _, services = create_mcp(events_settings(), store=store, slack=slack, auth=None)
    await services.event_processor.process(reaction_event(f"{time.time() - 600:.6f}", "EvGone"))

    [item] = await notifications_since(services)
    assert item["message"]["text"] == ""
    assert item["hydrated"] is False
    assert item["message"]["permalink"]


async def test_reply_event_parent_snippet_is_stored_and_shown(
    fake_slack: FakeSlack, slack: SlackClient
) -> None:
    store = InMemoryStore()
    _, services = create_mcp(events_settings(), store=store, slack=slack, auth=None)
    now = time.time()
    parent_ts = f"{now - 3600:.6f}"
    fake_slack.add(CHANNEL, msg(parent_ts, ME, "my question to the team"))
    reply = {
        "type": "message",
        "channel": CHANNEL,
        "user": BOB,
        "text": "here is the answer",
        "ts": f"{now - 60:.6f}",
        "thread_ts": parent_ts,
    }
    await services.event_processor.process(envelope(reply, "EvAns"))
    [stored] = await store.list_events("0", 10)
    assert stored.payload["parent"] == {
        "ts": parent_ts,
        "user": ME,
        "text": "my question to the team",
    }

    fake_slack.calls.clear()
    [item] = await notifications_since(services)
    assert item["type"] == "reply_to_me"
    assert item["parent"]["snippet"] == "my question to the team"
    assert item["parent"]["author"] == "Me Operator"
    # The 1d window also polls the gap before events went live; what must not happen is
    # a single-message fetch of the parent, whose text was stored with the event.
    single = [
        c
        for c in fake_slack.calls
        if c[0] == "conversations.history" and c[1].get("oldest") == c[1].get("latest")
    ]
    assert single == []
    assert not [c for c in fake_slack.calls if c[0] == "conversations.replies"]


async def test_events_stored_before_the_fix_get_their_parent_fetched(
    fake_slack: FakeSlack, slack: SlackClient
) -> None:
    """Events already in Firestore carry a parent without text; it is fetched on read."""
    from slack_guest_reader.store import StoredEvent

    store = InMemoryStore()
    _, services = create_mcp(events_settings(), store=store, slack=slack, auth=None)
    now = time.time()
    parent_ts = f"{now - 3600:.6f}"
    reply_ts = f"{now - 60:.6f}"
    fake_slack.add(CHANNEL, msg(parent_ts, ME, "older parent text"))
    await store.set_meta("events", {"first_received_at": now - 7200, "last_received_at": now})
    await store.put_event(
        StoredEvent(
            event_id="EvOld",
            event_ts=reply_ts,
            channel=CHANNEL,
            kind="reply_to_me",
            payload={
                "message": {"ts": reply_ts, "thread_ts": parent_ts, "user": BOB, "text": "ok"},
                "parent": {"ts": parent_ts, "user": ME},
            },
        ),
        3600,
    )
    [item] = await notifications_since(services, since="1h")
    assert item["parent"]["snippet"] == "older parent text"
    assert "hydrated" not in item


async def test_poll_reaction_is_dropped_when_an_event_covers_the_message(
    fake_slack: FakeSlack, slack: SlackClient
) -> None:
    """Gap polling and an event can both report a reaction on the same message."""
    store = InMemoryStore()
    _, services = create_mcp(events_settings(), store=store, slack=slack, auth=None)
    now = datetime.now(UTC)
    my_ts = f"{(now - timedelta(hours=5)).timestamp():.6f}"
    fake_slack.add(
        CHANNEL, msg(my_ts, ME, "mine", reactions=[{"name": "+1", "count": 1, "users": [ALICE]}])
    )
    # Events went live 1 h ago, so the 1d window polls the gap before that.
    await store.set_meta(
        "events",
        {
            "first_received_at": (now - timedelta(hours=1)).timestamp(),
            "last_received_at": now.timestamp(),
        },
    )
    await services.event_processor.process(reaction_event(my_ts, "EvDup"))

    items = await notifications_since(services)
    assert [(n["type"], n["source"]) for n in items] == [("reaction", "event")]
