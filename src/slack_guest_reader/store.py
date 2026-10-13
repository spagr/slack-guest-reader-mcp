"""Persistent state: user-name cache, polling cursors, my-thread index and events.

`Store` is the interface; `InMemoryStore` backs tests and local runs,
`FirestoreStore` backs the Cloud Run deployment. Neither stores message text
beyond what the notifications feature needs (events keep the raw Slack event
so the assistant can read it back; they expire via the Firestore TTL policy).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

USER_CACHE_TTL_SECONDS = 24 * 60 * 60

# Firestore collection names. Set the TTL policy on EVENTS.expires_at (see docs/DEPLOY.md).
USERS = "users"
CURSORS = "cursors"
MY_THREADS = "my_threads"
EVENTS = "events"
META = "meta"


@dataclass(slots=True)
class StoredEvent:
    """A Slack event already classified as a notification for the operator."""

    event_id: str
    event_ts: str
    channel: str
    kind: str
    payload: dict[str, Any]
    received_at: float = field(default_factory=time.time)


class Store(Protocol):
    async def get_user(self, user_id: str) -> dict[str, Any] | None: ...
    async def put_user(self, user_id: str, data: dict[str, Any]) -> None: ...

    async def get_cursor(self, channel: str) -> str | None: ...
    async def set_cursor(self, channel: str, ts: str) -> None: ...

    async def get_my_threads(self, channel: str) -> set[str]: ...
    async def add_my_threads(self, channel: str, thread_ts: set[str]) -> None: ...

    async def put_event(self, event: StoredEvent, ttl_seconds: int) -> bool:
        """Store an event. Returns False (and stores nothing) if event_id already exists."""
        ...

    async def list_events(self, since_ts: str, limit: int) -> list[StoredEvent]:
        """Events with event_ts > since_ts, newest first."""
        ...

    async def get_meta(self, key: str) -> dict[str, Any] | None: ...
    async def set_meta(self, key: str, data: dict[str, Any]) -> None: ...


class InMemoryStore:
    """Dictionary-backed store for tests and local development."""

    def __init__(self) -> None:
        self._users: dict[str, tuple[float, dict[str, Any]]] = {}
        self._cursors: dict[str, str] = {}
        self._threads: dict[str, set[str]] = {}
        self._events: dict[str, StoredEvent] = {}
        self._meta: dict[str, dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def get_user(self, user_id: str) -> dict[str, Any] | None:
        entry = self._users.get(user_id)
        if entry is None:
            return None
        expires_at, data = entry
        if expires_at < time.time():
            self._users.pop(user_id, None)
            return None
        return dict(data)

    async def put_user(self, user_id: str, data: dict[str, Any]) -> None:
        self._users[user_id] = (time.time() + USER_CACHE_TTL_SECONDS, dict(data))

    async def get_cursor(self, channel: str) -> str | None:
        return self._cursors.get(channel)

    async def set_cursor(self, channel: str, ts: str) -> None:
        self._cursors[channel] = ts

    async def get_my_threads(self, channel: str) -> set[str]:
        return set(self._threads.get(channel, set()))

    async def add_my_threads(self, channel: str, thread_ts: set[str]) -> None:
        self._threads.setdefault(channel, set()).update(thread_ts)

    async def put_event(self, event: StoredEvent, ttl_seconds: int) -> bool:
        async with self._lock:
            if event.event_id in self._events:
                return False
            self._events[event.event_id] = event
            return True

    async def list_events(self, since_ts: str, limit: int) -> list[StoredEvent]:
        rows = [e for e in self._events.values() if float(e.event_ts) > float(since_ts)]
        rows.sort(key=lambda e: float(e.event_ts), reverse=True)
        return rows[:limit]

    async def get_meta(self, key: str) -> dict[str, Any] | None:
        data = self._meta.get(key)
        return dict(data) if data is not None else None

    async def set_meta(self, key: str, data: dict[str, Any]) -> None:
        self._meta[key] = dict(data)


class FirestoreStore:
    """Firestore (Native mode) implementation. Verified only against a real project."""

    def __init__(self, client: Any) -> None:
        # `client` is a google.cloud.firestore.AsyncClient; typed as Any because the
        # library ships without complete type information.
        self._db = client

    @staticmethod
    def _expires(ttl_seconds: int) -> Any:
        from datetime import UTC, datetime, timedelta

        return datetime.now(UTC) + timedelta(seconds=ttl_seconds)

    async def get_user(self, user_id: str) -> dict[str, Any] | None:
        snap = await self._db.collection(USERS).document(user_id).get()
        if not snap.exists:
            return None
        doc = snap.to_dict() or {}
        expires_at = doc.get("expires_at")
        if expires_at is not None and expires_at.timestamp() < time.time():
            return None
        data = doc.get("data")
        return dict(data) if isinstance(data, dict) else None

    async def put_user(self, user_id: str, data: dict[str, Any]) -> None:
        await (
            self._db.collection(USERS)
            .document(user_id)
            .set({"data": data, "expires_at": self._expires(USER_CACHE_TTL_SECONDS)})
        )

    async def get_cursor(self, channel: str) -> str | None:
        snap = await self._db.collection(CURSORS).document(channel).get()
        if not snap.exists:
            return None
        value = (snap.to_dict() or {}).get("ts")
        return str(value) if value else None

    async def set_cursor(self, channel: str, ts: str) -> None:
        await self._db.collection(CURSORS).document(channel).set({"ts": ts})

    async def get_my_threads(self, channel: str) -> set[str]:
        snap = await self._db.collection(MY_THREADS).document(channel).get()
        if not snap.exists:
            return set()
        values = (snap.to_dict() or {}).get("ts", [])
        return {str(v) for v in values}

    async def add_my_threads(self, channel: str, thread_ts: set[str]) -> None:
        if not thread_ts:
            return
        from google.cloud.firestore import ArrayUnion

        await (
            self._db.collection(MY_THREADS)
            .document(channel)
            .set({"ts": ArrayUnion(sorted(thread_ts))}, merge=True)
        )

    async def put_event(self, event: StoredEvent, ttl_seconds: int) -> bool:
        from google.api_core.exceptions import AlreadyExists

        doc = {
            "event_id": event.event_id,
            "event_ts": event.event_ts,
            "event_ts_num": float(event.event_ts),
            "channel": event.channel,
            "kind": event.kind,
            "payload": event.payload,
            "received_at": event.received_at,
            "expires_at": self._expires(ttl_seconds),
        }
        try:
            await self._db.collection(EVENTS).document(event.event_id).create(doc)
        except AlreadyExists:
            return False
        return True

    async def list_events(self, since_ts: str, limit: int) -> list[StoredEvent]:
        from google.cloud.firestore import FieldFilter, Query

        query = (
            self._db.collection(EVENTS)
            .where(filter=FieldFilter("event_ts_num", ">", float(since_ts)))
            .order_by("event_ts_num", direction=Query.DESCENDING)
            .limit(limit)
        )
        rows: list[StoredEvent] = []
        async for snap in query.stream():
            doc = snap.to_dict() or {}
            rows.append(
                StoredEvent(
                    event_id=str(doc["event_id"]),
                    event_ts=str(doc["event_ts"]),
                    channel=str(doc["channel"]),
                    kind=str(doc["kind"]),
                    payload=dict(doc.get("payload") or {}),
                    received_at=float(doc.get("received_at") or 0.0),
                )
            )
        return rows

    async def get_meta(self, key: str) -> dict[str, Any] | None:
        snap = await self._db.collection(META).document(key).get()
        if not snap.exists:
            return None
        return dict(snap.to_dict() or {})

    async def set_meta(self, key: str, data: dict[str, Any]) -> None:
        await self._db.collection(META).document(key).set(data)
