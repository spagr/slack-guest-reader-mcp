"""Resolve Slack user IDs to display names, cached in the store for 24 h."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from typing import Any

from .render import USER_MENTION_RE
from .slack_client import SlackApiError, SlackClient
from .store import Store

logger = logging.getLogger(__name__)


def display_name(user: dict[str, Any]) -> str:
    profile = user.get("profile") or {}
    for candidate in (
        profile.get("display_name"),
        profile.get("real_name"),
        user.get("real_name"),
        user.get("name"),
    ):
        if candidate:
            return str(candidate)
    return str(user.get("id", "unknown"))


class UserResolver:
    def __init__(self, slack: SlackClient, store: Store, *, max_concurrency: int = 4) -> None:
        self._slack = slack
        self._store = store
        self._sem = asyncio.Semaphore(max_concurrency)

    async def resolve(self, user_ids: Iterable[str]) -> dict[str, str]:
        """Return {user_id: name}. Unknown or unresolvable IDs are omitted."""
        wanted = {uid for uid in user_ids if uid}
        names: dict[str, str] = {}
        missing: list[str] = []
        for uid in sorted(wanted):
            cached = await self._store.get_user(uid)
            if cached and cached.get("name"):
                names[uid] = str(cached["name"])
            else:
                missing.append(uid)

        async def fetch(uid: str) -> None:
            async with self._sem:
                try:
                    user = await self._slack.user_info(uid)
                except SlackApiError as exc:
                    logger.info("users.info failed for a user: %s", exc.error)
                    return
                name = display_name(user)
                names[uid] = name
                await self._store.put_user(uid, {"name": name, "is_bot": bool(user.get("is_bot"))})

        if missing:
            await asyncio.gather(*(fetch(uid) for uid in missing))
        return names

    async def names_for_messages(self, messages: Iterable[dict[str, Any]]) -> dict[str, str]:
        ids: set[str] = set()
        for msg in messages:
            if msg.get("user"):
                ids.add(str(msg["user"]))
            ids.update(uid for uid, _ in USER_MENTION_RE.findall(str(msg.get("text") or "")))
            for reaction in msg.get("reactions") or []:
                ids.update(str(u) for u in reaction.get("users") or [])
        return await self.resolve(ids)
