"""Integration tests: MCP tools over the in-memory transport against the fake Slack API."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from tests.conftest import ALICE, BOB, CHANNEL, ME, OTHER, PRIVATE, FakeSlack, body, msg

from slack_guest_reader.app import create_mcp
from slack_guest_reader.config import Settings
from slack_guest_reader.slack_client import SlackClient
from slack_guest_reader.store import InMemoryStore


def ts(now: datetime, **delta: float) -> str:
    return f"{(now - timedelta(**delta)).timestamp():.6f}"


@pytest.fixture
def now() -> datetime:
    return datetime.now(UTC)


@pytest.fixture
def workspace(fake_slack: FakeSlack, now: datetime) -> FakeSlack:
    fake_slack.add(
        CHANNEL, msg(ts(now, days=3), ALICE, "Kickoff notes <https://example.com/doc|doc>")
    )
    fake_slack.add_reply(CHANNEL, ts(now, days=3), msg(ts(now, days=2), ME, "thanks"))
    fake_slack.add_reply(CHANNEL, ts(now, days=3), msg(ts(now, hours=3), BOB, "Budget approved"))
    fake_slack.add(CHANNEL, msg(ts(now, hours=5), BOB, f"<@{ME}> can you review the budget?"))
    fake_slack.add(PRIVATE, msg(ts(now, hours=1), ALICE, "private note about budget"))
    fake_slack.add(OTHER, msg(ts(now, minutes=1), ALICE, f"<@{ME}> secret in other channel"))
    return fake_slack


@pytest.fixture
async def client(settings: Settings, workspace: FakeSlack, slack: SlackClient) -> Any:
    mcp, _ = create_mcp(settings, store=InMemoryStore(), slack=slack, auth=None)
    async with Client(mcp) as c:
        yield c


async def test_tools_are_read_only_and_described(client: Client) -> None:  # type: ignore[type-arg]
    tools = {t.name: t for t in await client.list_tools()}
    assert set(tools) == {
        "list_channels",
        "get_messages",
        "get_thread",
        "search_messages",
        "get_notifications",
    }
    for tool in tools.values():
        assert tool.annotations is not None and tool.annotations.read_only_hint is True
        assert "read-only" in (tool.description or "").lower()


async def test_list_channels(client: Client) -> None:  # type: ignore[type-arg]
    out = body(await client.call_tool("list_channels", {}))
    assert [c["id"] for c in out] == [CHANNEL, PRIVATE]
    assert out[0]["name"] == "project-alpha"
    assert out[0]["member_count"] == 12
    assert out[1]["is_private"] is True


async def test_get_messages_renders_and_bounds(client: Client, now: datetime) -> None:  # type: ignore[type-arg]
    out = body(await client.call_tool("get_messages", {"channel": "#project-alpha", "since": "7d"}))
    assert out["channel"] == CHANNEL
    assert out["count"] == 2
    newest, oldest = out["messages"]
    assert newest["author"]["name"] == "Bob B"
    assert newest["text"] == "@Me Operator can you review the budget?"
    assert oldest["text"] == "Kickoff notes doc (https://example.com/doc)"
    assert oldest["reply_count"] == 2
    assert oldest["permalink"].startswith("https://example-team.slack.com/archives/")
    assert newest["time"].endswith(("+01:00", "+02:00"))  # Europe/Prague

    limited = body(await client.call_tool("get_messages", {"channel": CHANNEL, "limit": 1}))
    assert limited["count"] == 1
    windowed = body(
        await client.call_tool("get_messages", {"channel": CHANNEL, "since": "1d", "until": "4h"})
    )
    assert [m["author"]["name"] for m in windowed["messages"]] == ["Bob B"]


async def test_allowlist_is_enforced_everywhere(client: Client) -> None:  # type: ignore[type-arg]
    with pytest.raises(ToolError, match="not in this server's allowlist"):
        await client.call_tool("get_messages", {"channel": OTHER})
    with pytest.raises(ToolError, match="allowlist"):
        await client.call_tool("get_thread", {"channel": "#random", "thread_ts": "1.0"})
    with pytest.raises(ToolError, match="allowlist"):
        await client.call_tool("search_messages", {"query": "secret", "channels": [OTHER]})
    hits = body(await client.call_tool("search_messages", {"query": "secret"}))
    assert hits["count"] == 0


async def test_get_thread(client: Client, now: datetime) -> None:  # type: ignore[type-arg]
    out = body(
        await client.call_tool("get_thread", {"channel": CHANNEL, "thread_ts": ts(now, days=3)})
    )
    assert out["parent"]["author"]["name"] == "Alice"
    assert [r["text"] for r in out["replies"]] == ["thanks", "Budget approved"]
    assert out["reply_count"] == 2
    assert out["replies"][0]["thread_ts"] == ts(now, days=3)


async def test_search_messages_includes_thread_replies(client: Client) -> None:  # type: ignore[type-arg]
    out = body(await client.call_tool("search_messages", {"query": "budget"}))
    assert [h["text"] for h in out["results"]] == [
        "private note about budget",
        "Budget approved",
        "@Me Operator can you review the budget?",
    ]
    phrase = body(await client.call_tool("search_messages", {"query": '"budget approved"'}))
    assert [h["text"] for h in phrase["results"]] == ["Budget approved"]
    scoped = body(
        await client.call_tool(
            "search_messages", {"query": "budget", "channels": ["#alpha-private"], "limit": 5}
        )
    )
    assert scoped["count"] == 1
    with pytest.raises(ToolError, match="must not be empty"):
        await client.call_tool("search_messages", {"query": "   "})


async def test_get_notifications_polling(client: Client, workspace: FakeSlack) -> None:  # type: ignore[type-arg]
    out = body(await client.call_tool("get_notifications", {}))
    assert [(n["type"], n["message"]["text"]) for n in out["notifications"]] == [
        ("thread_activity", "Budget approved"),
        ("mention", "@Me Operator can you review the budget?"),
    ]
    reply = out["notifications"][0]
    assert reply["parent"]["author"] == "Alice"
    assert reply["parent"]["snippet"].startswith("Kickoff notes")
    assert reply["source"] == "poll"

    # Cursor advanced: a second call without `since` only returns what's new.
    again = body(await client.call_tool("get_notifications", {}))
    assert again["count"] == 0
    explicit = body(await client.call_tool("get_notifications", {"since": "2d"}))
    assert explicit["count"] == 2


async def test_bad_time_values_give_clear_errors(client: Client) -> None:  # type: ignore[type-arg]
    with pytest.raises(ToolError, match="Cannot parse time"):
        await client.call_tool("get_messages", {"channel": CHANNEL, "since": "last tuesday"})
    with pytest.raises(ToolError, match="'until' must be later"):
        await client.call_tool("get_messages", {"channel": CHANNEL, "since": "1d", "until": "2d"})


async def test_healthz_route_exposes_no_data(settings: Settings, slack: SlackClient) -> None:
    import httpx

    from slack_guest_reader.app import create_app

    app = create_app(settings, store=InMemoryStore(), slack=slack, enable_auth=False)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            response = await http.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
