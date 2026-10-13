"""MCP tool definitions. All tools are read-only and enforce the channel allowlist."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from .config import Settings
from .notifications import NotificationPoller, NotificationRules, RawNotification
from .render import format_message, snippet
from .slack_client import SlackApiError, SlackClient, time_budget
from .store import Store, StoredEvent
from .timeparse import parse_time, to_slack_ts
from .users import UserResolver

logger = logging.getLogger(__name__)

MAX_LIMIT = 200
CHANNEL_INFO_TTL_SECONDS = 3600
SEARCH_SCAN_PER_CHANNEL = 500
SEARCH_THREADS_PER_CHANNEL = 30


@dataclass
class Services:
    settings: Settings
    slack: SlackClient
    store: Store
    resolver: UserResolver
    poller: NotificationPoller
    team_domain: str | None = None
    event_processor: Any = None
    _channel_cache: dict[str, tuple[float, dict[str, Any]]] = field(default_factory=dict)

    @classmethod
    def build(cls, settings: Settings, slack: SlackClient, store: Store) -> Services:
        resolver = UserResolver(slack, store)
        rules = NotificationRules(
            me_user_id=settings.me_user_id,
            thread_activity=settings.notify_thread_activity,
            group_mentions=settings.notify_group_mentions,
        )
        poller = NotificationPoller(
            slack,
            store,
            rules=rules,
            channels=settings.slack_channel_allowlist,
            thread_lookback_days=settings.thread_lookback_days,
        )
        return cls(settings=settings, slack=slack, store=store, resolver=resolver, poller=poller)

    @property
    def rules(self) -> NotificationRules:
        return self.poller._rules

    async def ensure_team_domain(self) -> str | None:
        if self.team_domain is None:
            try:
                info = await self.slack.auth_test()
            except SlackApiError as exc:
                logger.warning("auth.test failed: %s", exc.error)
                return None
            url = str(info.get("url") or "")  # e.g. https://example.slack.com/
            host = url.removeprefix("https://").split("/", 1)[0]
            self.team_domain = host.removesuffix(".slack.com") or None
            logger.info("slack team=%s bot=%s", info.get("team_id"), info.get("user_id"))
        return self.team_domain

    async def channel_info(self, channel: str) -> dict[str, Any]:
        cached = self._channel_cache.get(channel)
        if cached and cached[0] > time.monotonic():
            return cached[1]
        info = await self.slack.channel_info(channel)
        self._channel_cache[channel] = (time.monotonic() + CHANNEL_INFO_TTL_SECONDS, info)
        return info

    async def resolve_channel(self, value: str) -> str:
        """Map an ID or #name to an allowlisted channel ID, or raise a clear ToolError."""
        candidate = value.strip()
        if candidate in self.settings.channel_set:
            return candidate
        name = candidate.lstrip("#").lower()
        for channel_id in self.settings.slack_channel_allowlist:
            try:
                info = await self.channel_info(channel_id)
            except SlackApiError:
                continue
            if str(info.get("name", "")).lower() == name:
                return channel_id
        raise ToolError(
            f"Channel '{value}' is not in this server's allowlist. "
            "Call list_channels to see which channels are readable."
        )

    async def render(
        self, messages: list[dict[str, Any]], channel: str, names: dict[str, str] | None = None
    ) -> list[dict[str, Any]]:
        if names is None:
            names = await self.resolver.names_for_messages(messages)
        team_domain = await self.ensure_team_domain()
        return [
            format_message(
                msg,
                channel=channel,
                names=names,
                tz=self.settings.tzinfo,
                me_user_id=self.settings.me_user_id,
                team_domain=team_domain,
            )
            for msg in messages
        ]


def clamp_limit(limit: int, default: int) -> int:
    if limit <= 0:
        return default
    return min(limit, MAX_LIMIT)


def _window(
    settings: Settings, since: str | None, until: str | None, default_since: str | None
) -> tuple[datetime | None, datetime | None]:
    tz = settings.tzinfo
    effective_since = since or default_since
    try:
        since_dt = parse_time(effective_since, tz=tz) if effective_since else None
        until_dt = parse_time(until, tz=tz) if until else None
    except ValueError as exc:
        raise ToolError(str(exc)) from None
    if since_dt and until_dt and until_dt <= since_dt:
        raise ToolError("'until' must be later than 'since'.")
    return since_dt, until_dt


def _matches(text: str, terms: list[str], phrase: str | None) -> bool:
    lowered = text.lower()
    if phrase is not None:
        return phrase in lowered
    return all(term in lowered for term in terms)


def _parse_query(query: str) -> tuple[list[str], str | None]:
    q = query.strip()
    if len(q) >= 2 and q[0] == q[-1] == '"':
        return [], q[1:-1].lower()
    terms = [t.lower() for t in q.split() if t]
    if not terms:
        raise ToolError("Query must not be empty.")
    return terms, None


def register_tools(mcp: FastMCP, services: Services) -> None:
    settings = services.settings
    read_only = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": True}

    @mcp.tool(annotations=read_only)
    async def list_channels() -> list[dict[str, Any]]:
        """List the Slack channels this server is allowed to read.

        Returns id, name, topic, purpose and member count for every allowlisted channel.
        Use the `id` (or `#name`) with the other tools. Everything on this server is
        read-only; channels outside this list cannot be read at all.
        """
        with time_budget():
            out: list[dict[str, Any]] = []
            for channel_id in settings.slack_channel_allowlist:
                try:
                    info = await services.channel_info(channel_id)
                except SlackApiError as exc:
                    out.append({"id": channel_id, "error": exc.error})
                    continue
                out.append(
                    {
                        "id": channel_id,
                        "name": info.get("name"),
                        "is_private": bool(info.get("is_private")),
                        "topic": (info.get("topic") or {}).get("value") or "",
                        "purpose": (info.get("purpose") or {}).get("value") or "",
                        "member_count": info.get("num_members"),
                    }
                )
            return out

    @mcp.tool(annotations=read_only)
    async def get_messages(
        channel: Annotated[str, Field(description="Channel ID (C…) or #name from list_channels")],
        since: Annotated[
            str | None,
            Field(description="ISO 8601 date/time or relative value like '3d', '12h'. Default: 7d"),
        ] = None,
        until: Annotated[str | None, Field(description="ISO 8601 or relative; default now")] = None,
        limit: Annotated[int, Field(description="Max messages, newest first (max 200)")] = 50,
    ) -> dict[str, Any]:
        """Read top-level messages from one allowlisted channel, newest first.

        Thread replies are not included (except replies broadcast to the channel);
        use get_thread with a message's `ts` to read its replies. `reply_count` tells
        you whether a thread exists. Read-only.
        """
        limit = clamp_limit(limit, 50)
        with time_budget():
            channel_id = await services.resolve_channel(channel)
            since_dt, until_dt = _window(settings, since, until, "7d")
            try:
                raw = await services.slack.history(
                    channel_id,
                    oldest=to_slack_ts(since_dt) if since_dt else None,
                    latest=to_slack_ts(until_dt) if until_dt else None,
                    limit=limit,
                )
            except SlackApiError as exc:
                raise ToolError(f"Slack error reading channel: {exc.error}") from None
            messages = await services.render(raw, channel_id)
        return {"channel": channel_id, "count": len(messages), "messages": messages}

    @mcp.tool(annotations=read_only)
    async def get_thread(
        channel: Annotated[str, Field(description="Channel ID (C…) or #name")],
        thread_ts: Annotated[str, Field(description="`ts` of the parent message")],
        limit: Annotated[int, Field(description="Max replies, oldest first (max 200)")] = 100,
    ) -> dict[str, Any]:
        """Read one thread: the parent message followed by its replies, oldest first.

        Use when a message from get_messages, search_messages or get_notifications has
        `reply_count` > 0 or a `thread_ts`. Read-only.
        """
        limit = clamp_limit(limit, 100)
        with time_budget():
            channel_id = await services.resolve_channel(channel)
            try:
                raw = await services.slack.replies(channel_id, thread_ts.strip(), limit=limit)
            except SlackApiError as exc:
                raise ToolError(f"Slack error reading thread: {exc.error}") from None
            messages = await services.render(raw, channel_id)
        parent = messages[0] if messages else None
        return {
            "channel": channel_id,
            "thread_ts": thread_ts,
            "parent": parent,
            "replies": messages[1:],
            "reply_count": len(messages) - 1 if messages else 0,
        }

    @mcp.tool(annotations=read_only)
    async def search_messages(
        query: Annotated[
            str,
            Field(description='Keywords (all must match) or a "quoted phrase"; case-insensitive'),
        ],
        channels: Annotated[
            list[str] | None, Field(description="Channel IDs or #names; default all allowlisted")
        ] = None,
        since: Annotated[str, Field(description="ISO 8601 or relative, default '14d'")] = "14d",
        limit: Annotated[int, Field(description="Max results, newest first (max 200)")] = 30,
    ) -> dict[str, Any]:
        """Search recent messages in the allowlisted channels by keyword or phrase.

        The search runs on this server over fetched history (Slack's own search needs
        a user token, which this server deliberately does not have), so keep `since`
        short for speed. Matching replies in threads active in the window are included.
        Read-only.
        """
        limit = clamp_limit(limit, 30)
        terms, phrase = _parse_query(query)
        with time_budget():
            targets = (
                [await services.resolve_channel(c) for c in channels]
                if channels
                else list(settings.slack_channel_allowlist)
            )
            since_dt, _ = _window(settings, since, None, "14d")
            assert since_dt is not None
            since_ts = to_slack_ts(since_dt)
            hits: list[tuple[str, dict[str, Any]]] = []
            truncated = False
            for channel_id in targets:
                try:
                    history = await services.slack.history(
                        channel_id, oldest=since_ts, limit=SEARCH_SCAN_PER_CHANNEL
                    )
                except SlackApiError as exc:
                    raise ToolError(f"Slack error reading channel: {exc.error}") from None
                if len(history) >= SEARCH_SCAN_PER_CHANNEL:
                    truncated = True
                for msg in history:
                    if _matches(str(msg.get("text") or ""), terms, phrase):
                        hits.append((channel_id, msg))
                threads = [
                    m
                    for m in history
                    if m.get("reply_count") and float(m.get("latest_reply") or 0) > float(since_ts)
                ]
                threads.sort(key=lambda m: float(m.get("latest_reply") or 0), reverse=True)
                if len(threads) > SEARCH_THREADS_PER_CHANNEL:
                    truncated = True
                for parent in threads[:SEARCH_THREADS_PER_CHANNEL]:
                    try:
                        replies = await services.slack.replies(
                            channel_id, str(parent["ts"]), oldest=since_ts, limit=200
                        )
                    except SlackApiError:
                        continue
                    for reply in replies:
                        if reply.get("ts") == parent.get("ts"):
                            continue
                        if _matches(str(reply.get("text") or ""), terms, phrase):
                            hits.append((channel_id, reply))
            hits.sort(key=lambda h: float(h[1].get("ts") or 0), reverse=True)
            hits = hits[:limit]
            names = await services.resolver.names_for_messages([m for _, m in hits])
            results = []
            for channel_id, msg in hits:
                results.extend(await services.render([msg], channel_id, names))
        return {
            "query": query,
            "since": since_dt.isoformat(timespec="seconds"),
            "count": len(results),
            "truncated": truncated,
            "results": results,
        }

    @mcp.tool(annotations=read_only)
    async def get_notifications(
        since: Annotated[
            str | None,
            Field(
                description="ISO 8601 or relative ('2d'). Default: since the last call "
                "(per-channel cursor), or 7d on the first call"
            ),
        ] = None,
        limit: Annotated[int, Field(description="Max notifications, newest first (max 200)")] = 50,
    ) -> dict[str, Any]:
        """What's new for the operator: mentions, replies to their messages, activity in
        threads they took part in, and reactions to their messages.

        Each item has a `type` (mention | reply_to_me | thread_activity | reaction), the
        message, and for replies a short `parent` snippet. Own messages are excluded.
        Without `since`, only what's new since the previous call is returned. Read-only.
        """
        limit = clamp_limit(limit, 50)
        with time_budget(40):
            raw = await collect_notifications(services, since=since, limit=limit)
            items = await render_notifications(services, raw)
        return {"count": len(items), "notifications": items}


async def _default_since(services: Services, now: datetime) -> datetime:
    """Oldest per-channel cursor, or 7 days back when a channel was never polled."""
    fallback = now - timedelta(days=7)
    oldest: datetime | None = None
    for channel in services.settings.slack_channel_allowlist:
        cursor = await services.store.get_cursor(channel)
        if cursor is None:
            return fallback
        moment = datetime.fromtimestamp(float(cursor), tz=UTC)
        oldest = moment if oldest is None or moment < oldest else oldest
    return oldest or fallback


async def collect_notifications(
    services: Services, *, since: str | None, limit: int, now: datetime | None = None
) -> list[RawNotification]:
    """Merge stored events (phase 2) with on-demand polling (phase 1)."""
    settings = services.settings
    now = now or datetime.now(UTC)
    if since:
        try:
            since_dt = parse_time(since, tz=settings.tzinfo, now=now)
        except ValueError as exc:
            raise ToolError(str(exc)) from None
    else:
        since_dt = await _default_since(services, now)
    since_ts = to_slack_ts(since_dt)

    events_meta = await services.store.get_meta("events")
    events_live_from = (
        datetime.fromtimestamp(float(events_meta["first_received_at"]), tz=UTC)
        if events_meta and events_meta.get("first_received_at")
        else None
    )
    found: list[RawNotification] = []
    if events_live_from is not None:
        stored = await services.store.list_events(since_ts, limit)
        found.extend(_from_stored_event(e) for e in stored)
        # Poll only the gap before the events receiver came online.
        if since_dt < events_live_from:
            found.extend(
                await services.poller.poll(since=since_dt, until=events_live_from, now=now)
            )
        else:
            for channel in settings.slack_channel_allowlist:
                await services.store.set_cursor(channel, to_slack_ts(now))
    else:
        found.extend(await services.poller.poll(since=since_dt, now=now))

    seen: set[tuple[str, str, str, str]] = set()
    unique: list[RawNotification] = []
    for item in found:
        if item.key in seen:
            continue
        seen.add(item.key)
        unique.append(item)
    unique.sort(key=lambda n: n.sort_ts, reverse=True)
    return unique[:limit]


def _from_stored_event(event: StoredEvent) -> RawNotification:
    payload = event.payload
    if event.kind == "reaction":
        return RawNotification(
            "reaction",
            event.channel,
            payload.get("message") or {"ts": payload.get("item_ts")},
            reaction=str(payload.get("reaction") or ""),
            reacting_user=str(payload.get("user") or ""),
            source="event",
        )
    return RawNotification(
        event.kind,  # type: ignore[arg-type]  # validated when stored
        event.channel,
        dict(payload.get("message") or {}),
        parent=payload.get("parent"),
        source="event",
    )


async def render_notifications(
    services: Services, items: list[RawNotification]
) -> list[dict[str, Any]]:
    # Reaction events only carry the message ts; fetch the message so the text is shown.
    for item in items:
        if item.type == "reaction" and not item.message.get("text") and item.message.get("ts"):
            try:
                fetched = await services.slack.message_at(item.channel, str(item.message["ts"]))
            except SlackApiError:
                fetched = None
            if fetched:
                item.message = fetched
    all_messages = [n.message for n in items] + [n.parent for n in items if n.parent]
    for item in items:
        if item.reacting_user:
            all_messages.append({"user": item.reacting_user})
    names = await services.resolver.names_for_messages(all_messages)
    out = []
    for item in items:
        rendered = (await services.render([item.message], item.channel, names))[0]
        entry: dict[str, Any] = {"type": item.type, "message": rendered}
        if item.parent:
            parent_text = str(item.parent.get("text") or "")
            entry["parent"] = {
                "ts": item.parent.get("ts"),
                "author": names.get(str(item.parent.get("user") or ""), item.parent.get("user")),
                "snippet": snippet(parent_text),
            }
        if item.reactions is not None:
            entry["reactions"] = [
                {
                    "name": r["name"],
                    "count": r["count"],
                    "by": [names.get(u, u) for u in r.get("users", [])],
                }
                for r in item.reactions
            ]
        if item.reaction:
            entry["reaction"] = {
                "name": item.reaction,
                "by": names.get(item.reacting_user or "", item.reacting_user),
            }
        entry["source"] = item.source
        out.append(entry)
    return out
