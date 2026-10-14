"""FirestoreStore and the OAuth key-value backend against the Firestore emulator.

Both escaped the mock-only suite once: the production bug was a client_id shaped
like a URL (Client ID Metadata Document) used verbatim as a Firestore document ID.
Skipped unless FIRESTORE_EMULATOR_HOST is set; see tests/emulator.py.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from tests.conftest import CHANNEL, PRIVATE, make_settings
from tests.emulator import EMULATOR_PROJECT, firestore_client, requires_emulator

from slack_guest_reader.app import build_oauth_backend
from slack_guest_reader.auth import build_client_storage
from slack_guest_reader.store import EVENTS, MY_THREADS, USERS, FirestoreStore, StoredEvent

__all__ = ["firestore_client"]

pytestmark = [requires_emulator, pytest.mark.emulator]

CLAUDE_CLIENT_ID = "https://claude.ai/oauth/mcp-oauth-client-metadata"


@pytest.fixture
def fs_store(firestore_client: Any) -> FirestoreStore:
    return FirestoreStore(firestore_client)


# --- users cache ---


async def test_user_cache_round_trip_and_miss(fs_store: FirestoreStore) -> None:
    assert await fs_store.get_user("U0000000002") is None
    await fs_store.put_user("U0000000002", {"name": "alice", "display": "Alice"})
    assert await fs_store.get_user("U0000000002") == {"name": "alice", "display": "Alice"}


async def test_user_cache_expired_entry_is_a_miss(
    fs_store: FirestoreStore, firestore_client: Any
) -> None:
    await (
        firestore_client.collection(USERS)
        .document("U0000000003")
        .set({"data": {"name": "bob"}, "expires_at": datetime.now(UTC) - timedelta(seconds=1)})
    )
    assert await fs_store.get_user("U0000000003") is None


async def test_user_cache_ttl_is_about_a_day(
    fs_store: FirestoreStore, firestore_client: Any
) -> None:
    await fs_store.put_user("U0000000002", {"name": "alice"})
    snap = await firestore_client.collection(USERS).document("U0000000002").get()
    remaining = snap.to_dict()["expires_at"].timestamp() - time.time()
    assert 23 * 3600 < remaining <= 24 * 3600


# --- cursors ---


async def test_cursor_round_trip(fs_store: FirestoreStore) -> None:
    assert await fs_store.get_cursor(CHANNEL) is None
    await fs_store.set_cursor(CHANNEL, "1700000000.000100")
    await fs_store.set_cursor(CHANNEL, "1700000001.000200")
    assert await fs_store.get_cursor(CHANNEL) == "1700000001.000200"
    assert await fs_store.get_cursor(PRIVATE) is None


# --- my threads (ArrayUnion) ---


async def test_my_threads_array_union_merges_and_dedupes(
    fs_store: FirestoreStore, firestore_client: Any
) -> None:
    assert await fs_store.get_my_threads(CHANNEL) == set()
    await fs_store.add_my_threads(CHANNEL, {"1.1", "2.2"})
    await fs_store.add_my_threads(CHANNEL, {"2.2", "3.3"})
    await fs_store.add_my_threads(CHANNEL, set())  # no-op, must not fail or clear
    assert await fs_store.get_my_threads(CHANNEL) == {"1.1", "2.2", "3.3"}
    assert await fs_store.get_my_threads(PRIVATE) == set()
    snap = await firestore_client.collection(MY_THREADS).document(CHANNEL).get()
    assert sorted(snap.to_dict()["ts"]) == ["1.1", "2.2", "3.3"]


async def test_my_threads_merge_keeps_other_fields(
    fs_store: FirestoreStore, firestore_client: Any
) -> None:
    await firestore_client.collection(MY_THREADS).document(CHANNEL).set({"note": "keep"})
    await fs_store.add_my_threads(CHANNEL, {"9.9"})
    doc = (await firestore_client.collection(MY_THREADS).document(CHANNEL).get()).to_dict()
    assert doc == {"note": "keep", "ts": ["9.9"]}


# --- events ---


def event(event_id: str, ts: str, kind: str = "mention") -> StoredEvent:
    return StoredEvent(
        event_id=event_id,
        event_ts=ts,
        channel=CHANNEL,
        kind=kind,
        payload={"type": "message", "ts": ts, "user": "U0000000002", "text": "hi"},
        received_at=1700000000.0,
    )


async def test_put_event_is_idempotent_on_event_id(
    fs_store: FirestoreStore, firestore_client: Any
) -> None:
    assert await fs_store.put_event(event("Ev01", "1700000001.000100"), ttl_seconds=3600) is True
    assert await fs_store.put_event(event("Ev01", "1700000001.000100"), ttl_seconds=3600) is False
    snap = await firestore_client.collection(EVENTS).document("Ev01").get()
    doc = snap.to_dict()
    assert doc["event_ts_num"] == pytest.approx(1700000001.0001)
    assert doc["kind"] == "mention"
    remaining = doc["expires_at"].timestamp() - time.time()
    assert 3500 < remaining <= 3600


async def test_list_events_orders_newest_first_and_bounds(fs_store: FirestoreStore) -> None:
    for i, ts in enumerate(["1700000003.0", "1700000001.0", "1700000005.0", "1700000002.0"]):
        assert await fs_store.put_event(event(f"Ev{i}", ts), ttl_seconds=3600)

    rows = await fs_store.list_events("1700000000.0", limit=10)
    assert [r.event_ts for r in rows] == [
        "1700000005.0",
        "1700000003.0",
        "1700000002.0",
        "1700000001.0",
    ]
    assert rows[0].event_id == "Ev2"
    assert rows[0].payload["text"] == "hi"
    assert rows[0].received_at == 1700000000.0

    since = await fs_store.list_events("1700000002.0", limit=10)
    assert [r.event_ts for r in since] == ["1700000005.0", "1700000003.0"]

    limited = await fs_store.list_events("1700000000.0", limit=2)
    assert [r.event_ts for r in limited] == ["1700000005.0", "1700000003.0"]

    assert await fs_store.list_events("1700000005.0", limit=10) == []


# --- meta ---


async def test_meta_round_trip(fs_store: FirestoreStore) -> None:
    assert await fs_store.get_meta("bot") is None
    await fs_store.set_meta("bot", {"team_id": "T0000000000"})
    assert await fs_store.get_meta("bot") == {"team_id": "T0000000000"}


# --- OAuth key-value backend (FastMCP client/token storage) ---


@pytest.fixture
def oauth_settings() -> Any:
    return make_settings(FIRESTORE_ENABLED="true", GOOGLE_CLOUD_PROJECT=EMULATOR_PROJECT)


async def oauth_document_ids(firestore_client: Any, collection: str = "oauth") -> list[str]:
    return [doc.id async for doc in firestore_client.collection(collection).list_documents()]


@pytest.mark.parametrize(
    "key",
    [
        CLAUDE_CLIENT_ID,
        "https://example.com/oauth/client-metadata",
        "a/b.c",
        "../up",
        ".",
        "..",
        "__reserved__",
        "plain-key.with.dots",
    ],
)
async def test_oauth_backend_round_trips_url_shaped_keys(
    oauth_settings: Any, firestore_client: Any, key: str
) -> None:
    backend = build_oauth_backend(oauth_settings)
    value = {"client_id": key, "redirect_uris": ["https://example.com/callback"]}

    assert await backend.get(key=key) is None
    await backend.put(key=key, value=value)
    assert await backend.get(key=key) == value
    assert await backend.delete(key=key) is True
    assert await backend.get(key=key) is None

    # The sanitized document ID is what Firestore saw; the raw key never was.
    await backend.put(key=key, value=value)
    ids = await oauth_document_ids(firestore_client)
    assert len(ids) == 1
    assert "/" not in ids[0]
    assert ids[0] not in (".", "..")
    if key != "plain-key.with.dots":
        assert ids[0] != key


async def test_oauth_backend_distinct_urls_do_not_collide(
    oauth_settings: Any, firestore_client: Any
) -> None:
    backend = build_oauth_backend(oauth_settings)
    a, b = "https://example.com/a/b", "https://example.com/a_b"
    await backend.put(key=a, value={"who": "a"})
    await backend.put(key=b, value={"who": "b"})
    assert await backend.get(key=a) == {"who": "a"}
    assert await backend.get(key=b) == {"who": "b"}
    assert len(await oauth_document_ids(firestore_client)) == 2


async def test_oauth_backend_collections_are_sanitized_too(
    oauth_settings: Any, firestore_client: Any
) -> None:
    backend = build_oauth_backend(oauth_settings)
    await backend.put(key=CLAUDE_CLIENT_ID, value={"ok": True}, collection="mcp/clients")
    assert await backend.get(key=CLAUDE_CLIENT_ID, collection="mcp/clients") == {"ok": True}
    names = [c.id async for c in firestore_client.collections()]
    assert names and all("/" not in name for name in names)


async def test_encrypted_client_storage_round_trips_through_firestore(
    oauth_settings: Any, firestore_client: Any
) -> None:
    """The real stack: FernetEncryptionWrapper over the sanitized Firestore store."""
    storage = build_client_storage(oauth_settings, build_oauth_backend(oauth_settings))
    secret = {"access_token": "ya29.not-a-real-token", "client_id": CLAUDE_CLIENT_ID}
    await storage.put(key=CLAUDE_CLIENT_ID, value=secret, ttl=600)
    assert await storage.get(key=CLAUDE_CLIENT_ID) == secret

    ids = await oauth_document_ids(firestore_client)
    assert len(ids) == 1 and "/" not in ids[0]
    raw = (await firestore_client.collection("oauth").document(ids[0]).get()).to_dict()
    assert "ya29.not-a-real-token" not in repr(raw)
