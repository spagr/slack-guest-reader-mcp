"""Opt-in get_file: images and PDFs attached to messages in allowlisted channels."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from tests.conftest import ALICE, CHANNEL, ME, OTHER, FakeSlack, make_settings, msg

from slack_guest_reader.app import create_mcp
from slack_guest_reader.files import file_kind, pick_source
from slack_guest_reader.render import format_message
from slack_guest_reader.slack_client import SlackClient
from slack_guest_reader.store import InMemoryStore

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.7\n" + b"0" * 64
TOKEN = "xoxb-TEST-TOKEN"


def slack_file(
    file_id: str,
    *,
    name: str = "image.png",
    mimetype: str = "image/png",
    filetype: str = "png",
    size: int = len(PNG),
    **extra: Any,
) -> dict[str, Any]:
    return {
        "id": file_id,
        "name": name,
        "mimetype": mimetype,
        "filetype": filetype,
        "size": size,
        "mode": "hosted",
        "url_private": f"https://files.slack.com/files-pri/T0-{file_id}/{name}",
        "permalink": f"https://example-team.slack.com/files/U0/{file_id}/{name}",
        **extra,
    }


class FileHost:
    """Serves files.slack.com (and anything else) and records who got the token."""

    def __init__(self) -> None:
        self.routes: dict[str, Callable[[httpx.Request], httpx.Response]] = {}
        self.requests: list[tuple[str, str | None]] = []

    def add(self, url: str, content: bytes, content_type: str) -> None:
        self.routes[url] = lambda _req: httpx.Response(
            200, content=content, headers={"content-type": content_type}
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append((url, request.headers.get("authorization")))
        route = self.routes.get(url)
        return route(request) if route else httpx.Response(404)


@pytest.fixture
def workspace() -> FakeSlack:
    return FakeSlack()


@pytest.fixture
def files_host() -> FileHost:
    return FileHost()


def make_slack(workspace: FakeSlack, files_host: FileHost) -> SlackClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "slack.com":
            return workspace.handler(request)
        return files_host.handle(request)

    async def no_sleep(_: float) -> None:
        return None

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return SlackClient(TOKEN, http=http, sleep=no_sleep)


@pytest.fixture
async def client(workspace: FakeSlack, files_host: FileHost) -> AsyncIterator[Any]:
    settings = make_settings(ENABLE_FILE_CONTENT="true", MAX_FILE_BYTES="200000")
    mcp, _ = create_mcp(
        settings, store=InMemoryStore(), slack=make_slack(workspace, files_host), auth=None
    )
    async with Client(mcp) as c:
        yield c


def now_ts(offset: float = 0) -> str:
    return f"{time.time() - 600 + offset:.6f}"


def split(result: Any) -> tuple[dict[str, Any], Any]:
    meta = json.loads(result.content[0].text)
    return meta, result.content[1]


# --- pure helpers ------------------------------------------------------------------


def test_file_kind() -> None:
    assert file_kind(slack_file("F1")) == "image"
    assert file_kind(slack_file("F2", mimetype="image/jpeg", filetype="jpg")) == "image"
    assert file_kind(slack_file("F3", mimetype="application/pdf", filetype="pdf")) == "pdf"
    assert file_kind(slack_file("F4", mimetype="text/csv", filetype="csv")) is None
    assert file_kind(slack_file("F5", mode="external", is_external=True)) is None
    assert file_kind(slack_file("F6", filetype="remote")) is None
    assert file_kind(slack_file("F7", mode="tombstone")) is None


def test_pick_source_prefers_original_then_thumbnail() -> None:
    small = slack_file("F1")
    assert pick_source(small, "image", 200_000) == (small["url_private"], "original")
    big = slack_file("F2", size=10_000_000, thumb_720="https://files.slack.com/t720.png")
    assert pick_source(big, "image", 200_000) == ("https://files.slack.com/t720.png", "thumb_720")
    with pytest.raises(ToolError):
        pick_source(slack_file("F3", size=10_000_000), "image", 200_000)
    with pytest.raises(ToolError):
        pick_source(slack_file("F4", mimetype="application/pdf", size=900_000), "pdf", 200_000)


def test_render_marks_readable_only_when_enabled() -> None:
    raw = msg("1700000000.000100", ALICE, "see attached")
    raw["files"] = [slack_file("F1"), slack_file("F2", mimetype="text/csv", filetype="csv")]
    kwargs: dict[str, Any] = {
        "channel": CHANNEL,
        "names": {},
        "tz": make_settings().tzinfo,
        "me_user_id": ME,
        "team_domain": "example-team",
    }
    off = format_message(raw, **kwargs)
    assert all("readable" not in f for f in off["files"])
    on = format_message(raw, **kwargs, file_content=True)
    assert [f.get("readable") for f in on["files"]] == [True, None]
    assert on["files"][0]["size"] == len(PNG)


# --- the tool ----------------------------------------------------------------------


async def test_tool_absent_when_disabled(workspace: FakeSlack, files_host: FileHost) -> None:
    mcp, _ = create_mcp(
        make_settings(),
        store=InMemoryStore(),
        slack=make_slack(workspace, files_host),
        auth=None,
    )
    async with Client(mcp) as c:
        assert "get_file" not in {t.name for t in await c.list_tools()}


async def test_image_on_top_level_message(
    client: Client,  # type: ignore[type-arg]
    workspace: FakeSlack,
    files_host: FileHost,
) -> None:
    ts = now_ts()
    file = slack_file("F0000000001")
    workspace.add(CHANNEL, {**msg(ts, ALICE, "screens below:"), "files": [file]})
    files_host.add(file["url_private"], PNG, "image/png")

    result = await client.call_tool(
        "get_file", {"channel": CHANNEL, "ts": ts, "file_id": "F0000000001"}
    )
    meta, image = split(result)
    assert meta["kind"] == "image" and meta["variant"] == "original"
    assert image.type == "image" and image.mime_type == "image/png"
    assert base64.b64decode(image.data) == PNG
    assert files_host.requests == [(file["url_private"], f"Bearer {TOKEN}")]


async def test_image_on_thread_reply_and_thumbnail_for_large_files(
    client: Client,  # type: ignore[type-arg]
    workspace: FakeSlack,
    files_host: FileHost,
) -> None:
    parent_ts, reply_ts = now_ts(), now_ts(60)
    thumb = "https://files.slack.com/files-tmb/T0-F0000000002/image_1024.png"
    file = slack_file("F0000000002", size=50_000_000, thumb_1024=thumb)
    workspace.add(CHANNEL, msg(parent_ts, ME, "parent"))
    workspace.add_reply(CHANNEL, parent_ts, {**msg(reply_ts, ALICE, "here:"), "files": [file]})
    files_host.add(thumb, PNG, "image/png")

    result = await client.call_tool(
        "get_file", {"channel": CHANNEL, "ts": reply_ts, "file_id": "F0000000002"}
    )
    meta, image = split(result)
    assert meta["variant"] == "thumb_1024"
    assert base64.b64decode(image.data) == PNG


async def test_pdf_is_returned_as_embedded_resource(
    client: Client,  # type: ignore[type-arg]
    workspace: FakeSlack,
    files_host: FileHost,
) -> None:
    ts = now_ts()
    file = slack_file(
        "F0000000003", name="spec.pdf", mimetype="application/pdf", filetype="pdf", size=len(PDF)
    )
    workspace.add(CHANNEL, {**msg(ts, ALICE, "the spec"), "files": [file]})
    files_host.add(file["url_private"], PDF, "application/pdf")

    result = await client.call_tool(
        "get_file", {"channel": CHANNEL, "ts": ts, "file_id": "F0000000003"}
    )
    meta, resource = split(result)
    assert meta["kind"] == "pdf"
    assert resource.type == "resource"
    assert resource.resource.mime_type == "application/pdf"
    assert base64.b64decode(resource.resource.blob) == PDF


async def test_file_must_belong_to_the_message_and_channel(
    client: Client,  # type: ignore[type-arg]
    workspace: FakeSlack,
    files_host: FileHost,
) -> None:
    ts = now_ts()
    file = slack_file("F0000000004")
    workspace.add(CHANNEL, {**msg(ts, ALICE, "with file"), "files": [file]})
    workspace.add(OTHER, {**msg(ts, ALICE, "elsewhere"), "files": [file]})
    files_host.add(file["url_private"], PNG, "image/png")

    with pytest.raises(ToolError, match="not attached"):
        await client.call_tool("get_file", {"channel": CHANNEL, "ts": ts, "file_id": "F0000009999"})
    with pytest.raises(ToolError, match="allowlist"):
        await client.call_tool("get_file", {"channel": OTHER, "ts": ts, "file_id": "F0000000004"})
    with pytest.raises(ToolError, match="No message"):
        await client.call_tool(
            "get_file", {"channel": CHANNEL, "ts": "1.000001", "file_id": "F0000000004"}
        )
    assert files_host.requests == []


async def test_token_never_leaves_slack_file_hosts(
    client: Client,  # type: ignore[type-arg]
    workspace: FakeSlack,
    files_host: FileHost,
) -> None:
    ts = now_ts()
    foreign = slack_file("F0000000005")
    foreign["url_private"] = "https://files.slack.com.example.com/evil.png"
    remote = slack_file(
        "F0000000006", mode="external", is_external=True, filetype="remote", mimetype=""
    )
    redirecting = slack_file("F0000000007")
    workspace.add(CHANNEL, {**msg(ts, ALICE, "files"), "files": [foreign, remote, redirecting]})
    cdn = "https://cdn.example.com/presigned/image.png"
    files_host.routes[redirecting["url_private"]] = lambda _req: httpx.Response(
        302, headers={"location": cdn}
    )
    files_host.add(cdn, PNG, "image/png")

    with pytest.raises(ToolError, match="not hosted by Slack"):
        await client.call_tool("get_file", {"channel": CHANNEL, "ts": ts, "file_id": "F0000000005"})
    with pytest.raises(ToolError, match="Only images"):
        await client.call_tool("get_file", {"channel": CHANNEL, "ts": ts, "file_id": "F0000000006"})
    # A redirect to another host is followed without the token.
    result = await client.call_tool(
        "get_file", {"channel": CHANNEL, "ts": ts, "file_id": "F0000000007"}
    )
    assert base64.b64decode(split(result)[1].data) == PNG
    assert files_host.requests == [
        (redirecting["url_private"], f"Bearer {TOKEN}"),
        (cdn, None),
    ]


async def test_missing_scope_and_size_limit_give_clear_errors(
    client: Client,  # type: ignore[type-arg]
    workspace: FakeSlack,
    files_host: FileHost,
) -> None:
    ts = now_ts()
    no_scope = slack_file("F0000000008")
    huge = slack_file("F0000000009", size=1000)  # Slack under-reports; the stream is cut off
    workspace.add(CHANNEL, {**msg(ts, ALICE, "files"), "files": [no_scope, huge]})
    # Without files:read Slack serves its HTML sign-in page with status 200.
    files_host.add(no_scope["url_private"], b"<html>sign in</html>", "text/html; charset=utf-8")
    files_host.add(huge["url_private"], b"\x89PNG" + b"0" * 300_000, "image/png")

    with pytest.raises(ToolError, match="files:read"):
        await client.call_tool("get_file", {"channel": CHANNEL, "ts": ts, "file_id": "F0000000008"})
    with pytest.raises(ToolError, match="larger than"):
        await client.call_tool("get_file", {"channel": CHANNEL, "ts": ts, "file_id": "F0000000009"})
