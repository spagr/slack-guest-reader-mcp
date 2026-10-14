"""Shared fixtures: placeholder settings, an in-memory store and a fake Slack API via respx."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
import respx

from slack_guest_reader.config import Settings
from slack_guest_reader.slack_client import SlackClient
from slack_guest_reader.store import InMemoryStore

ME = "U0000000001"
ALICE = "U0000000002"
BOB = "U0000000003"
CHANNEL = "C0000000001"
PRIVATE = "G0000000002"
OTHER = "C0000000099"  # not allowlisted

BASE_ENV = {
    "SLACK_BOT_TOKEN": "xoxb-TEST-TOKEN",
    "SLACK_CHANNEL_ALLOWLIST": f"{CHANNEL},{PRIVATE}",
    "ME_USER_ID": ME,
    "GOOGLE_CLIENT_ID": "000000000000-test.apps.googleusercontent.com",
    "GOOGLE_CLIENT_SECRET": "GOCSPX-REPLACE-ME",
    "ALLOWED_EMAILS": "operator@example.com",
    "PUBLIC_BASE_URL": "https://mcp.example.com",
    "FIRESTORE_ENABLED": "false",
    "TIMEZONE": "Europe/Prague",
}


def make_settings(**overrides: str) -> Settings:
    env = {**BASE_ENV, **overrides}
    return Settings(_env_file=None, **{k.lower(): v for k, v in env.items()})  # type: ignore[arg-type]


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def store() -> InMemoryStore:
    return InMemoryStore()


def msg(
    ts: str,
    user: str,
    text: str,
    *,
    thread_ts: str | None = None,
    reply_count: int = 0,
    latest_reply: str | None = None,
    reactions: list[dict[str, Any]] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    out: dict[str, Any] = {"type": "message", "ts": ts, "user": user, "text": text}
    if thread_ts:
        out["thread_ts"] = thread_ts
    if reply_count:
        out["reply_count"] = reply_count
        out["latest_reply"] = latest_reply
    if reactions:
        out["reactions"] = reactions
    out.update(extra)
    return out


class FakeSlack:
    """Minimal in-memory Slack Web API: history, replies, info, users, auth."""

    def __init__(self) -> None:
        self.channels: dict[str, dict[str, Any]] = {
            CHANNEL: {
                "id": CHANNEL,
                "name": "project-alpha",
                "is_private": False,
                "topic": {"value": "Alpha topic"},
                "purpose": {"value": "Alpha"},
                "num_members": 12,
            },
            PRIVATE: {
                "id": PRIVATE,
                "name": "alpha-private",
                "is_private": True,
                "topic": {"value": ""},
                "purpose": {"value": ""},
                "num_members": 3,
            },
            OTHER: {
                "id": OTHER,
                "name": "random",
                "is_private": False,
                "topic": {"value": ""},
                "purpose": {"value": ""},
                "num_members": 50,
            },
        }
        self.users: dict[str, dict[str, Any]] = {
            ME: {"id": ME, "name": "me", "profile": {"display_name": "Me Operator"}},
            ALICE: {"id": ALICE, "name": "alice", "profile": {"display_name": "Alice"}},
            BOB: {"id": BOB, "name": "bob", "profile": {"display_name": "", "real_name": "Bob B"}},
        }
        # channel -> list of top-level messages; threads -> replies keyed by (channel, ts)
        self.messages: dict[str, list[dict[str, Any]]] = {CHANNEL: [], PRIVATE: [], OTHER: []}
        self.replies: dict[tuple[str, str], list[dict[str, Any]]] = {}
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.page_size_cap = 200

    # --- building ---
    def add(self, channel: str, message: dict[str, Any]) -> None:
        self.messages.setdefault(channel, []).append(message)

    def add_reply(self, channel: str, parent_ts: str, reply: dict[str, Any]) -> None:
        reply.setdefault("thread_ts", parent_ts)
        self.replies.setdefault((channel, parent_ts), []).append(reply)
        for m in self.messages[channel]:
            if m["ts"] == parent_ts:
                rs = self.replies[(channel, parent_ts)]
                m["reply_count"] = len(rs)
                m["latest_reply"] = max(r["ts"] for r in rs)

    # --- handler ---
    def handler(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.calls.append((method, form))
        if request.headers.get("Authorization") != "Bearer xoxb-TEST-TOKEN":
            return httpx.Response(200, json={"ok": False, "error": "invalid_auth"})
        fn = getattr(self, f"_{method.replace('.', '_')}", None)
        if fn is None:
            return httpx.Response(200, json={"ok": False, "error": "unknown_method"})
        return httpx.Response(200, json=fn(form))

    @staticmethod
    def _paged(items: list[dict[str, Any]], form: dict[str, str], cap: int) -> dict[str, Any]:
        limit = min(int(form.get("limit", "100")), cap)
        start = int(form.get("cursor", "0") or 0)
        page = items[start : start + limit]
        has_more = start + limit < len(items)
        return {
            "ok": True,
            "messages": page,
            "has_more": has_more,
            "response_metadata": {"next_cursor": str(start + limit) if has_more else ""},
        }

    def _conversations_history(self, form: dict[str, str]) -> dict[str, Any]:
        channel = form["channel"]
        if channel not in self.channels:
            return {"ok": False, "error": "channel_not_found"}
        oldest = float(form.get("oldest", "0") or 0)
        latest = float(form.get("latest", "0") or 0) or None
        inclusive = form.get("inclusive") == "true"
        rows = []
        for m in self.messages.get(channel, []):
            ts = float(m["ts"])
            if inclusive:
                if ts < oldest or (latest and ts > latest):
                    continue
            elif ts <= oldest or (latest and ts >= latest):
                continue
            rows.append(m)
        rows.sort(key=lambda m: float(m["ts"]), reverse=True)
        return self._paged(rows, form, self.page_size_cap)

    def _conversations_replies(self, form: dict[str, str]) -> dict[str, Any]:
        channel, ts = form["channel"], form["ts"]
        parent = next((m for m in self.messages.get(channel, []) if m["ts"] == ts), None)
        if parent is None:
            return {"ok": False, "error": "thread_not_found"}
        oldest = float(form.get("oldest", "0") or 0)
        rows = [parent] + [
            r for r in self.replies.get((channel, ts), []) if float(r["ts"]) > oldest
        ]
        return self._paged(rows, form, self.page_size_cap)

    def _conversations_info(self, form: dict[str, str]) -> dict[str, Any]:
        info = self.channels.get(form["channel"])
        return (
            {"ok": True, "channel": info} if info else {"ok": False, "error": "channel_not_found"}
        )

    def _users_info(self, form: dict[str, str]) -> dict[str, Any]:
        user = self.users.get(form["user"])
        return {"ok": True, "user": user} if user else {"ok": False, "error": "user_not_found"}

    def _auth_test(self, form: dict[str, str]) -> dict[str, Any]:
        return {
            "ok": True,
            "url": "https://example-team.slack.com/",
            "team_id": "T0000000000",
            "user_id": "U0000000BOT",
        }


@pytest.fixture
def fake_slack() -> Any:
    fake = FakeSlack()
    with respx.mock(base_url="https://slack.com/api", assert_all_called=False) as router:
        router.post(path__regex=r"/.*").mock(side_effect=fake.handler)
        yield fake


@pytest.fixture
def slack(fake_slack: FakeSlack) -> SlackClient:
    async def no_sleep(_: float) -> None:
        return None

    return SlackClient("xoxb-TEST-TOKEN", sleep=no_sleep)


def body(response: Any) -> Any:
    """Decode a FastMCP CallToolResult into plain JSON."""
    if response.structured_content is not None:
        sc = response.structured_content
        return sc["result"] if isinstance(sc, dict) and set(sc) == {"result"} else sc
    return json.loads(response.content[0].text)


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "emulator: needs the Firestore emulator (FIRESTORE_EMULATOR_HOST); skipped otherwise",
    )
