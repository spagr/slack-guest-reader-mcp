"""'What's new for me': classification rules plus the polling implementation.

Classification is pure (`classify_message`, `classify_reaction`) so it can be
unit-tested and shared by the polling path (phase 1) and the events receiver
(phase 2). `NotificationPoller` computes notifications on demand from
`conversations.history` and `conversations.replies`, keeping a per-channel
cursor and an index of threads the operator took part in.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from .render import has_group_mention, mentions_user
from .timeparse import to_slack_ts

logger = logging.getLogger(__name__)

NotificationType = Literal["mention", "reply_to_me", "thread_activity", "reaction"]

# Subtypes that are not real content and never count as notifications.
IGNORED_SUBTYPES = frozenset(
    {
        "channel_join",
        "channel_leave",
        "channel_topic",
        "channel_purpose",
        "channel_name",
        "channel_archive",
        "channel_unarchive",
        "group_join",
        "group_leave",
        "message_changed",
        "message_deleted",
        "tombstone",
        "bot_add",
        "bot_remove",
        "pinned_item",
        "unpinned_item",
    }
)


@dataclass(frozen=True, slots=True)
class NotificationRules:
    me_user_id: str
    thread_activity: bool = True
    group_mentions: bool = False


def is_thread_reply(msg: dict[str, Any]) -> bool:
    thread_ts = msg.get("thread_ts")
    return bool(thread_ts) and thread_ts != msg.get("ts")


def classify_message(
    msg: dict[str, Any],
    rules: NotificationRules,
    *,
    my_threads: set[str],
    parent_author: str | None = None,
) -> NotificationType | None:
    """Return the most specific notification type for a message, or None.

    Priority: mention > reply_to_me > thread_activity. Own messages and
    housekeeping subtypes never notify.
    """
    if msg.get("user") == rules.me_user_id:
        return None
    if msg.get("subtype") in IGNORED_SUBTYPES:
        return None
    text = str(msg.get("text") or "")
    if mentions_user(text, rules.me_user_id):
        return "mention"
    if rules.group_mentions and has_group_mention(text):
        return "mention"
    if is_thread_reply(msg):
        if parent_author == rules.me_user_id:
            return "reply_to_me"
        if rules.thread_activity and str(msg.get("thread_ts")) in my_threads:
            return "thread_activity"
    return None


def classify_reaction(
    *, reacting_user: str, item_author: str | None, rules: NotificationRules
) -> NotificationType | None:
    """A reaction notifies when someone else reacts to a message the operator wrote."""
    if reacting_user == rules.me_user_id:
        return None
    if item_author != rules.me_user_id:
        return None
    return "reaction"


def reactions_from_others(msg: dict[str, Any], me_user_id: str) -> list[dict[str, Any]]:
    """Reactions on a message that were not (only) added by the operator."""
    out = []
    for reaction in msg.get("reactions") or []:
        users = [u for u in reaction.get("users") or [] if u != me_user_id]
        others = int(reaction.get("count") or 0) - (
            1 if me_user_id in (reaction.get("users") or []) else 0
        )
        if others > 0:
            out.append({"name": reaction.get("name"), "count": others, "users": users})
    return out


@dataclass(slots=True)
class RawNotification:
    """A notification before rendering: raw message dicts plus context."""

    type: NotificationType
    channel: str
    message: dict[str, Any]
    parent: dict[str, Any] | None = None
    reactions: list[dict[str, Any]] | None = None
    reaction: str | None = None
    reacting_user: str | None = None
    source: str = "poll"

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.type, self.channel, str(self.message.get("ts", "")), self.reaction or "")

    @property
    def sort_ts(self) -> float:
        return float(self.message.get("ts") or 0)


class NotificationPoller:
    """Phase 1: derive notifications from history + replies within a time window."""

    def __init__(
        self,
        slack: Any,
        store: Any,
        *,
        rules: NotificationRules,
        channels: list[str],
        thread_lookback_days: int = 14,
        max_messages_per_channel: int = 400,
        max_threads_per_channel: int = 40,
    ) -> None:
        self._slack = slack
        self._store = store
        self._rules = rules
        self._channels = channels
        self._lookback = timedelta(days=thread_lookback_days)
        self._max_messages = max_messages_per_channel
        self._max_threads = max_threads_per_channel

    async def poll(
        self,
        *,
        since: datetime,
        until: datetime | None = None,
        now: datetime | None = None,
        advance_cursor: bool = True,
    ) -> list[RawNotification]:
        now = now or datetime.now(UTC)
        found: list[RawNotification] = []
        for channel in self._channels:
            found.extend(
                await self._poll_channel(
                    channel, since=since, until=until, now=now, advance_cursor=advance_cursor
                )
            )
        found.sort(key=lambda n: n.sort_ts, reverse=True)
        return found

    async def _poll_channel(
        self,
        channel: str,
        *,
        since: datetime,
        until: datetime | None,
        now: datetime,
        advance_cursor: bool,
    ) -> list[RawNotification]:
        me = self._rules.me_user_id
        since_ts = to_slack_ts(since)
        until_ts = to_slack_ts(until) if until else None
        # Old threads can get new replies, so scan parents back to the lookback window.
        scan_from = min(since, now - self._lookback)
        history = await self._slack.history(
            channel, oldest=to_slack_ts(scan_from), latest=until_ts, limit=self._max_messages
        )
        my_threads = await self._store.get_my_threads(channel)
        newest_seen = 0.0
        found: list[RawNotification] = []
        thread_candidates: list[dict[str, Any]] = []
        new_my_threads: set[str] = set()

        for msg in history:
            ts = float(msg.get("ts") or 0)
            newest_seen = max(newest_seen, ts)
            if msg.get("user") == me:
                # Threads I started or replied to (broadcast replies show up in history too).
                if msg.get("reply_count") or msg.get("thread_ts"):
                    new_my_threads.add(str(msg.get("thread_ts") or msg.get("ts")))
                if ts > float(since_ts):
                    others = reactions_from_others(msg, me)
                    if others:
                        found.append(RawNotification("reaction", channel, msg, reactions=others))
            elif ts > float(since_ts) and not is_thread_reply(msg):
                kind = classify_message(msg, self._rules, my_threads=my_threads)
                if kind:
                    found.append(RawNotification(kind, channel, msg))
            latest_reply = float(msg.get("latest_reply") or 0)
            if msg.get("reply_count") and latest_reply > float(since_ts):
                thread_candidates.append(msg)

        my_threads |= new_my_threads
        thread_candidates.sort(key=lambda m: float(m.get("latest_reply") or 0), reverse=True)
        for parent in thread_candidates[: self._max_threads]:
            parent_ts = str(parent["ts"])
            # For threads not yet known to involve me, read the whole thread once so my
            # earlier replies are discovered; afterwards only the delta is fetched.
            known = parent_ts in my_threads or parent.get("user") == me
            replies = await self._slack.replies(
                channel, parent_ts, oldest=since_ts if known else None, limit=self._max_messages
            )
            parent_author = str(parent.get("user") or "")
            if any(r.get("user") == me and str(r.get("ts")) != parent_ts for r in replies):
                new_my_threads.add(parent_ts)
                my_threads.add(parent_ts)
            for reply in replies:
                if str(reply.get("ts")) == parent_ts:
                    continue
                reply_ts = float(reply.get("ts") or 0)
                if reply_ts <= float(since_ts) or (until_ts and reply_ts > float(until_ts)):
                    continue
                newest_seen = max(newest_seen, reply_ts)
                if reply.get("user") == me:
                    continue
                kind = classify_message(
                    reply, self._rules, my_threads=my_threads, parent_author=parent_author
                )
                if kind:
                    found.append(RawNotification(kind, channel, reply, parent=parent))

        if new_my_threads:
            await self._store.add_my_threads(channel, new_my_threads)
        if advance_cursor and newest_seen > 0:
            await self._store.set_cursor(channel, f"{newest_seen:.6f}")
        logger.info(
            "poll channel=%s history=%d threads=%d notifications=%d",
            channel,
            len(history),
            min(len(thread_candidates), self._max_threads),
            len(found),
        )
        return found
