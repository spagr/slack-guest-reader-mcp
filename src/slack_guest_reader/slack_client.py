"""Thin async client for the handful of Slack Web API read methods we use.

Handles pagination (`cursor`), HTTP 429 (`Retry-After`), exponential backoff
with jitter on 5xx, and a per-tool-call time budget. Never logs message text
or tokens, only method names, counts and error codes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import time
from collections.abc import Awaitable, Callable, Iterator
from contextvars import ContextVar
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

logger = logging.getLogger(__name__)

SLACK_API = "https://slack.com/api"
DEFAULT_BUDGET_SECONDS = 25.0
PAGE_SIZE = 200  # Slack allows up to 999 for history/replies; 200 keeps responses small

_deadline: ContextVar[float | None] = ContextVar("slack_deadline", default=None)


class SlackApiError(Exception):
    """Slack answered with ok=false (or an unrecoverable HTTP status)."""

    def __init__(self, error: str, method: str, status: int = 200) -> None:
        super().__init__(f"{method}: {error} (HTTP {status})")
        self.error = error
        self.method = method
        self.status = status


class SlackBudgetExceeded(SlackApiError):
    """The per-call time budget ran out while waiting on Slack."""

    def __init__(self, method: str) -> None:
        super().__init__("time_budget_exceeded", method, 0)


# Hosts that may receive the bot token on a file download (docs.slack.dev, Working with
# files: compare the parsed host exactly, never a prefix).
SLACK_FILE_HOSTS = frozenset({"files.slack.com"})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
MAX_REDIRECTS = 3


class FileDownloadError(Exception):
    """A file could not be downloaded; `code` is short and safe to log or show."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def is_slack_file_url(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme == "https" and (parts.hostname or "") in SLACK_FILE_HOSTS


@contextlib.contextmanager
def time_budget(seconds: float = DEFAULT_BUDGET_SECONDS) -> Iterator[None]:
    """Bound the total time spent in Slack calls (including retries) for one tool call."""
    token = _deadline.set(time.monotonic() + seconds)
    try:
        yield
    finally:
        _deadline.reset(token)


def _remaining() -> float | None:
    deadline = _deadline.get()
    return None if deadline is None else deadline - time.monotonic()


class SlackClient:
    def __init__(
        self,
        token: str,
        http: httpx.AsyncClient | None = None,
        *,
        base_url: str = SLACK_API,
        max_retries: int = 4,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._token = token
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(15.0))
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        self._sleep = sleep

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ core

    async def call(self, method: str, **params: Any) -> dict[str, Any]:
        """POST a Web API method, returning the decoded body. Raises SlackApiError on ok=false."""
        data = {k: v for k, v in params.items() if v is not None}
        url = f"{self._base_url}/{method}"
        headers = {"Authorization": f"Bearer {self._token}"}
        attempt = 0
        while True:
            remaining = _remaining()
            if remaining is not None and remaining <= 0:
                raise SlackBudgetExceeded(method)
            started = time.monotonic()
            try:
                response = await self._http.post(url, data=data, headers=headers)
            except httpx.TransportError as exc:
                await self._backoff(method, attempt, reason=type(exc).__name__)
                attempt += 1
                continue
            elapsed_ms = int((time.monotonic() - started) * 1000)

            if response.status_code == 429:
                retry_after = float(response.headers.get("Retry-After", "1") or 1)
                logger.warning("slack %s rate limited, retry_after=%ss", method, retry_after)
                await self._wait(method, retry_after)
                attempt += 1
                continue
            if response.status_code >= 500:
                await self._backoff(method, attempt, reason=f"http_{response.status_code}")
                attempt += 1
                continue
            if response.status_code != 200:
                raise SlackApiError(f"http_{response.status_code}", method, response.status_code)

            body: dict[str, Any] = response.json()
            if not body.get("ok"):
                error = str(body.get("error", "unknown_error"))
                logger.warning("slack %s failed: %s (%dms)", method, error, elapsed_ms)
                raise SlackApiError(error, method)
            logger.debug("slack %s ok (%dms)", method, elapsed_ms)
            return body

    async def _backoff(self, method: str, attempt: int, *, reason: str) -> None:
        if attempt >= self._max_retries:
            raise SlackApiError(reason, method, 503)
        delay = min(8.0, 0.5 * (2**attempt)) + random.uniform(0, 0.5)  # noqa: S311
        logger.warning("slack %s %s, retry %d in %.1fs", method, reason, attempt + 1, delay)
        await self._wait(method, delay)

    async def _wait(self, method: str, seconds: float) -> None:
        remaining = _remaining()
        if remaining is not None and seconds > remaining:
            raise SlackBudgetExceeded(method)
        await self._sleep(seconds)

    async def _paginate(
        self, method: str, key: str, limit: int, **params: Any
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        while len(items) < limit:
            page_size = min(PAGE_SIZE, limit - len(items))
            body = await self.call(method, limit=page_size, cursor=cursor, **params)
            items.extend(body.get(key, []))
            cursor = (body.get("response_metadata") or {}).get("next_cursor") or None
            if not cursor or not body.get("has_more", bool(cursor)):
                break
        return items[:limit]

    # --------------------------------------------------------------- methods

    async def history(
        self,
        channel: str,
        *,
        oldest: str | None = None,
        latest: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Top-level messages (plus broadcast replies), newest first, bounded by oldest/latest."""
        return await self._paginate(
            "conversations.history",
            "messages",
            limit,
            channel=channel,
            oldest=oldest,
            latest=latest,
            inclusive="false",
        )

    async def replies(
        self,
        channel: str,
        thread_ts: str,
        *,
        oldest: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Thread parent followed by replies, oldest first. `oldest` filters replies only."""
        messages = await self._paginate(
            "conversations.replies",
            "messages",
            limit + 1,  # the parent counts against Slack's limit
            channel=channel,
            ts=thread_ts,
            oldest=oldest,
            inclusive="true",
        )
        return messages

    async def message_at(self, channel: str, ts: str) -> dict[str, Any] | None:
        """Fetch exactly one message by its ts, top-level or a reply in a thread.

        Used to hydrate event notifications: `reaction_added` carries only the item's
        ts, not its text or `thread_ts`, and replies never appear in
        `conversations.history`. `conversations.replies` accepts the ts of any message
        in a thread, so it is the fallback; both calls are bounded to that one ts.
        """
        body = await self.call(
            "conversations.history",
            channel=channel,
            latest=ts,
            oldest=ts,
            inclusive="true",
            limit=1,
        )
        for message in body.get("messages", []):
            if message.get("ts") == ts:
                return dict(message)
        try:
            body = await self.call(
                "conversations.replies",
                channel=channel,
                ts=ts,
                latest=ts,
                oldest=ts,
                inclusive="true",
                limit=2,
            )
        except SlackApiError as exc:
            if exc.error in {"thread_not_found", "message_not_found"}:
                return None
            raise
        for message in body.get("messages", []):
            if message.get("ts") == ts:
                return dict(message)
        return None

    async def download(
        self, url: str, *, max_bytes: int, accept: Callable[[str], bool]
    ) -> tuple[bytes, str]:
        """GET a Slack-hosted file; return (content, content type). Needs `files:read`.

        The bot token is only ever sent to a Slack file host. Redirects are followed by
        hand: a target on a Slack file host gets the token again, any other https target
        is requested without it (as the Slack docs require). The body is streamed and
        cut off above `max_bytes`. Slack answers a token without `files:read` with an
        HTML sign-in page, which `accept` rejects.
        """
        if not is_slack_file_url(url):
            raise FileDownloadError("untrusted_host")
        for _ in range(MAX_REDIRECTS + 1):
            remaining = _remaining()
            if remaining is not None and remaining <= 0:
                raise FileDownloadError("time_budget_exceeded")
            timeout = min(remaining, 15.0) if remaining is not None else 15.0
            parts = urlsplit(url)
            if parts.scheme != "https":
                raise FileDownloadError("insecure_redirect")
            headers = {"Authorization": f"Bearer {self._token}"} if is_slack_file_url(url) else {}
            try:
                async with self._http.stream(
                    "GET", url, headers=headers, follow_redirects=False, timeout=timeout
                ) as response:
                    if response.status_code in REDIRECT_STATUSES:
                        location = response.headers.get("location")
                        if not location:
                            raise FileDownloadError("bad_redirect")
                        url = urljoin(url, location)
                        continue
                    if response.status_code != 200:
                        raise FileDownloadError(f"http_{response.status_code}")
                    content_type = (
                        response.headers.get("content-type", "").split(";")[0].strip().lower()
                    )
                    if not accept(content_type):
                        raise FileDownloadError("unexpected_content_type")
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > max_bytes:
                        raise FileDownloadError("too_large")
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            raise FileDownloadError("too_large")
                        chunks.append(chunk)
                    return b"".join(chunks), content_type
            except httpx.HTTPError as exc:
                raise FileDownloadError(type(exc).__name__) from None
        raise FileDownloadError("too_many_redirects")

    async def channel_info(self, channel: str) -> dict[str, Any]:
        body = await self.call("conversations.info", channel=channel)
        channel_data: dict[str, Any] = body["channel"]
        return channel_data

    async def user_info(self, user: str) -> dict[str, Any]:
        body = await self.call("users.info", user=user)
        user_data: dict[str, Any] = body["user"]
        return user_data

    async def auth_test(self) -> dict[str, Any]:
        return await self.call("auth.test")
