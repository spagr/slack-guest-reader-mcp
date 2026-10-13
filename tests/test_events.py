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
    assert stored[0].payload["parent"] == {"ts": parent_ts, "user": ME}
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
