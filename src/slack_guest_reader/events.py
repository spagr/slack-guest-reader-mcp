"""Slack Events API receiver (phase 2).

`POST /slack/events` is not OAuth-protected; it verifies Slack's request
signature instead, answers the `url_verification` challenge, classifies
message and reaction events with the same rules as polling, and stores the
ones that matter in Firestore keyed by `event_id` (idempotent).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from typing import Any

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response

from .notifications import classify_message, classify_reaction, is_thread_reply
from .render import permalink, snippet
from .slack_client import SlackApiError, time_budget
from .store import StoredEvent

logger = logging.getLogger(__name__)

REPLAY_WINDOW_SECONDS = 5 * 60
EVENT_BUDGET_SECONDS = 2.5  # Slack wants an ack within 3 s
ALERTED_TYPES = frozenset({"mention", "reply_to_me"})


def verify_slack_signature(
    signing_secret: str,
    *,
    timestamp: str | None,
    body: bytes,
    signature: str | None,
    now: float | None = None,
) -> bool:
    """Constant-time check of `X-Slack-Signature` with a 5-minute replay window."""
    if not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs((now if now is not None else time.time()) - ts) > REPLAY_WINDOW_SECONDS:
        return False
    base = b"v0:" + timestamp.encode() + b":" + body
    expected = "v0=" + hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


class EventProcessor:
    """Classifies incoming events and persists the relevant ones."""

    def __init__(self, services: Any, *, ttl_seconds: int, alert_webhook_url: str | None) -> None:
        self._services = services
        self._ttl = ttl_seconds
        self._alert_url = alert_webhook_url
        self._parent_authors: dict[tuple[str, str], str] = {}

    @property
    def _channels(self) -> frozenset[str]:
        return frozenset(self._services.settings.channel_set)

    async def process(self, envelope: dict[str, Any]) -> str | None:
        """Return the notification type stored for this envelope, or None."""
        event = envelope.get("event") or {}
        event_id = str(envelope.get("event_id") or "")
        if not event_id:
            return None
        await self._mark_receiver_live()
        etype = event.get("type")
        if etype == "message":
            return await self._on_message(event_id, event)
        if etype == "reaction_added":
            return await self._on_reaction(event_id, event)
        return None

    async def _mark_receiver_live(self) -> None:
        meta = await self._services.store.get_meta("events")
        now = time.time()
        if meta and meta.get("first_received_at"):
            if now - float(meta.get("last_received_at") or 0) > 60:
                await self._services.store.set_meta("events", {**meta, "last_received_at": now})
            return
        await self._services.store.set_meta(
            "events", {"first_received_at": now, "last_received_at": now}
        )

    async def _parent_author(self, channel: str, thread_ts: str) -> str | None:
        key = (channel, thread_ts)
        if key in self._parent_authors:
            return self._parent_authors[key]
        try:
            messages = await self._services.slack.replies(channel, thread_ts, limit=1)
        except SlackApiError as exc:
            logger.info("could not load thread parent: %s", exc.error)
            return None
        author = str(messages[0].get("user") or "") if messages else None
        if author:
            if len(self._parent_authors) > 2000:
                self._parent_authors.clear()
            self._parent_authors[key] = author
        return author

    async def _on_message(self, event_id: str, event: dict[str, Any]) -> str | None:
        channel = str(event.get("channel") or "")
        if channel not in self._channels:
            return None
        rules = self._services.rules
        store = self._services.store
        thread_ts = str(event.get("thread_ts") or "")
        if event.get("user") == rules.me_user_id:
            if thread_ts:
                await store.add_my_threads(channel, {thread_ts})
            return None
        parent: dict[str, Any] | None = None
        parent_author: str | None = None
        my_threads: set[str] = set()
        if is_thread_reply(event):
            my_threads = await store.get_my_threads(channel)
            parent_author = await self._parent_author(channel, thread_ts)
            if parent_author:
                parent = {"ts": thread_ts, "user": parent_author}
        kind = classify_message(event, rules, my_threads=my_threads, parent_author=parent_author)
        if kind is None:
            return None
        message = {
            k: event.get(k)
            for k in ("ts", "thread_ts", "user", "text", "subtype", "files", "bot_id")
            if event.get(k) is not None
        }
        stored = await store.put_event(
            StoredEvent(
                event_id=event_id,
                event_ts=str(event.get("ts") or event.get("event_ts") or ""),
                channel=channel,
                kind=kind,
                payload={"message": message, "parent": parent},
            ),
            self._ttl,
        )
        if stored:
            await self._alert(
                kind,
                channel,
                str(message.get("ts") or ""),
                thread_ts or None,
                str(message.get("text") or ""),
            )
        return kind

    async def _on_reaction(self, event_id: str, event: dict[str, Any]) -> str | None:
        item = event.get("item") or {}
        channel = str(item.get("channel") or "")
        if item.get("type") != "message" or channel not in self._channels:
            return None
        kind = classify_reaction(
            reacting_user=str(event.get("user") or ""),
            item_author=str(event.get("item_user") or "") or None,
            rules=self._services.rules,
        )
        if kind is None:
            return None
        await self._services.store.put_event(
            StoredEvent(
                event_id=event_id,
                event_ts=str(event.get("event_ts") or ""),
                channel=channel,
                kind=kind,
                payload={
                    "reaction": event.get("reaction"),
                    "user": event.get("user"),
                    "item_ts": item.get("ts"),
                    "message": {"ts": item.get("ts"), "user": event.get("item_user")},
                },
            ),
            self._ttl,
        )
        return kind

    async def _alert(
        self, kind: str, channel: str, ts: str, thread_ts: str | None, text: str
    ) -> None:
        if not self._alert_url or kind not in ALERTED_TYPES:
            return
        team_domain = await self._services.ensure_team_domain()
        payload = {
            "type": kind,
            "channel": channel,
            "ts": ts,
            "permalink": permalink(team_domain, channel, ts, thread_ts),
            "snippet": snippet(text, 200),
        }
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                response = await client.post(self._alert_url, json=payload)
            logger.info("alert webhook status=%s", response.status_code)
        except httpx.HTTPError as exc:
            logger.warning("alert webhook failed: %s", type(exc).__name__)


def make_events_endpoint(signing_secret: str, processor: EventProcessor) -> Any:
    async def slack_events(request: Request) -> Response:
        body = await request.body()
        if not verify_slack_signature(
            signing_secret,
            timestamp=request.headers.get("X-Slack-Request-Timestamp"),
            body=body,
            signature=request.headers.get("X-Slack-Signature"),
        ):
            return PlainTextResponse("invalid signature", status_code=401)
        try:
            envelope = json.loads(body)
        except ValueError:
            return PlainTextResponse("invalid json", status_code=400)
        if envelope.get("type") == "url_verification":
            return JSONResponse({"challenge": envelope.get("challenge", "")})
        if envelope.get("type") != "event_callback":
            return PlainTextResponse("ignored", status_code=200)
        if request.headers.get("X-Slack-Retry-Num"):
            logger.info("slack retry #%s", request.headers.get("X-Slack-Retry-Num"))
        try:
            with time_budget(EVENT_BUDGET_SECONDS):
                kind = await processor.process(envelope)
        except Exception as exc:
            # 5xx makes Slack retry (up to 3 times); storage is idempotent by event_id.
            logger.error("event processing failed: %s", type(exc).__name__)
            return PlainTextResponse("retry", status_code=500)
        logger.info("event stored=%s", kind or "-")
        return PlainTextResponse("ok", status_code=200)

    return slack_events
