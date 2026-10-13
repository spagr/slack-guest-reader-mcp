"""Turn raw Slack message dicts into compact, LLM-friendly JSON.

Pure functions only: name resolution is passed in as a mapping so this module
has no I/O. See `users.py` for the resolver.
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

USER_MENTION_RE = re.compile(r"<@([UW][A-Z0-9]+)(?:\|([^>]*))?>")
CHANNEL_MENTION_RE = re.compile(r"<#([CG][A-Z0-9]+)(?:\|([^>]*))?>")
SPECIAL_MENTION_RE = re.compile(r"<!(here|channel|everyone)(?:\|[^>]*)?>")
SUBTEAM_MENTION_RE = re.compile(r"<!subteam\^(S[A-Z0-9]+)(?:\|([^>]*))?>")
LINK_RE = re.compile(r"<((?:https?|mailto):[^>|]+)(?:\|([^>]*))?>")
GROUP_MENTIONS = ("<!here", "<!channel", "<!everyone")


def user_ids_in(text: str) -> set[str]:
    return {uid for uid, _ in USER_MENTION_RE.findall(text)}


def mentions_user(text: str, user_id: str) -> bool:
    return any(uid == user_id for uid, _ in USER_MENTION_RE.findall(text))


def has_group_mention(text: str) -> bool:
    return any(token in text for token in GROUP_MENTIONS)


def render_text(
    text: str,
    names: Mapping[str, str],
    channel_names: Mapping[str, str] | None = None,
) -> str:
    """Resolve Slack mrkdwn entities into readable text; keep link labels and URLs."""
    channel_names = channel_names or {}

    def user(m: re.Match[str]) -> str:
        uid, label = m.group(1), m.group(2)
        return "@" + (names.get(uid) or label or uid)

    def channel(m: re.Match[str]) -> str:
        cid, label = m.group(1), m.group(2)
        return "#" + (label or channel_names.get(cid) or cid)

    def subteam(m: re.Match[str]) -> str:
        label = m.group(2)
        return label if label and label.startswith("@") else "@" + (label or m.group(1))

    def link(m: re.Match[str]) -> str:
        url, label = m.group(1), m.group(2)
        if label and label != url:
            return f"{label} ({url})"
        return url

    out = USER_MENTION_RE.sub(user, text)
    out = CHANNEL_MENTION_RE.sub(channel, out)
    out = SPECIAL_MENTION_RE.sub(lambda m: "@" + m.group(1), out)
    out = SUBTEAM_MENTION_RE.sub(subteam, out)
    out = LINK_RE.sub(link, out)
    return html.unescape(out)


def ts_to_iso(ts: str, tz: ZoneInfo) -> str:
    return datetime.fromtimestamp(float(ts), tz=tz).isoformat(timespec="seconds")


def permalink(
    team_domain: str | None, channel: str, ts: str, thread_ts: str | None = None
) -> str | None:
    """Build a permalink without an API call. Needs the team domain from auth.test."""
    if not team_domain:
        return None
    base = f"https://{team_domain}.slack.com/archives/{channel}/p{ts.replace('.', '')}"
    if thread_ts and thread_ts != ts:
        return f"{base}?thread_ts={thread_ts}&cid={channel}"
    return base


def snippet(text: str, limit: int = 140) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def format_message(
    raw: Mapping[str, Any],
    *,
    channel: str,
    names: Mapping[str, str],
    tz: ZoneInfo,
    me_user_id: str,
    team_domain: str | None,
    channel_names: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Compact JSON for one message. Files are metadata only; nothing is downloaded."""
    ts = str(raw.get("ts", ""))
    thread_ts_raw = raw.get("thread_ts")
    thread_ts = str(thread_ts_raw) if thread_ts_raw else None
    author_id = str(raw.get("user") or raw.get("bot_id") or "")
    bot_profile = raw.get("bot_profile") or {}
    author_name = names.get(author_id) or raw.get("username") or bot_profile.get("name")
    text = str(raw.get("text") or "")

    reactions = [
        {
            "name": str(r.get("name", "")),
            "count": int(r.get("count", 0)),
            "me": me_user_id in (r.get("users") or []),
        }
        for r in raw.get("reactions") or []
    ]
    files = [
        {
            "id": f.get("id"),
            "name": f.get("name") or f.get("title"),
            "type": f.get("filetype") or f.get("mimetype"),
            "permalink": f.get("permalink"),
        }
        for f in raw.get("files") or []
    ]

    out: dict[str, Any] = {
        "channel": channel,
        "ts": ts,
        "thread_ts": thread_ts,
        "author": {"id": author_id or None, "name": author_name or author_id or None},
        "time": ts_to_iso(ts, tz) if ts else None,
        "text": render_text(text, names, channel_names),
        "reply_count": int(raw.get("reply_count") or 0),
        "reactions": reactions,
        "files": files,
        "permalink": permalink(team_domain, channel, ts, thread_ts) if ts else None,
    }
    subtype = raw.get("subtype")
    if subtype:
        out["subtype"] = subtype
    if raw.get("edited"):
        out["edited"] = True
    return out
