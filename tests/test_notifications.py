from datetime import UTC, datetime, timedelta

from tests.conftest import ALICE, BOB, CHANNEL, ME, PRIVATE, FakeSlack, msg

from slack_guest_reader.notifications import (
    NotificationPoller,
    NotificationRules,
    classify_message,
    classify_reaction,
    reactions_from_others,
)
from slack_guest_reader.slack_client import SlackClient
from slack_guest_reader.store import InMemoryStore

RULES = NotificationRules(me_user_id=ME)


def test_mention() -> None:
    assert classify_message(msg("1.0", ALICE, f"hey <@{ME}>"), RULES, my_threads=set()) == "mention"


def test_group_mention_configurable() -> None:
    text = "<!channel> standup"
    assert classify_message(msg("1.0", ALICE, text), RULES, my_threads=set()) is None
    rules = NotificationRules(me_user_id=ME, group_mentions=True)
    assert classify_message(msg("1.0", ALICE, text), rules, my_threads=set()) == "mention"


def test_reply_to_me() -> None:
    reply = msg("2.0", ALICE, "sure", thread_ts="1.0")
    assert classify_message(reply, RULES, my_threads=set(), parent_author=ME) == "reply_to_me"
    assert classify_message(reply, RULES, my_threads=set(), parent_author=BOB) is None


def test_thread_activity_configurable() -> None:
    reply = msg("2.0", ALICE, "more", thread_ts="1.0")
    assert classify_message(reply, RULES, my_threads={"1.0"}, parent_author=BOB) == (
        "thread_activity"
    )
    quiet = NotificationRules(me_user_id=ME, thread_activity=False)
    assert classify_message(reply, quiet, my_threads={"1.0"}, parent_author=BOB) is None


def test_mention_wins_over_thread_rules() -> None:
    reply = msg("2.0", ALICE, f"<@{ME}> look", thread_ts="1.0")
    assert classify_message(reply, RULES, my_threads={"1.0"}, parent_author=ME) == "mention"


def test_own_messages_and_housekeeping_are_ignored() -> None:
    assert (
        classify_message(msg("1.0", ME, f"<@{ME}> note to self"), RULES, my_threads=set()) is None
    )
    joined = msg("1.0", ALICE, f"<@{ME}> has joined", subtype="channel_join")
    assert classify_message(joined, RULES, my_threads=set()) is None


def test_reactions() -> None:
    assert classify_reaction(reacting_user=ALICE, item_author=ME, rules=RULES) == "reaction"
    assert classify_reaction(reacting_user=ME, item_author=ME, rules=RULES) is None
    assert classify_reaction(reacting_user=ALICE, item_author=BOB, rules=RULES) is None
    mine = msg(
        "1.0",
        ME,
        "x",
        reactions=[
            {"name": "eyes", "count": 2, "users": [ME, ALICE]},
            {"name": "tada", "count": 1, "users": [ME]},
        ],
    )
    assert reactions_from_others(mine, ME) == [{"name": "eyes", "count": 1, "users": [ALICE]}]


def ts(now: datetime, **delta: float) -> str:
    return f"{(now - timedelta(**delta)).timestamp():.6f}"


async def test_poller_finds_all_four_types_and_advances_cursor(
    fake_slack: FakeSlack, slack: SlackClient
) -> None:
    now = datetime.now(UTC)
    store = InMemoryStore()
    # Old thread I started, new reply after `since` -> reply_to_me (parent older than since).
    fake_slack.add(CHANNEL, msg(ts(now, days=10), ME, "my old question"))
    fake_slack.add_reply(CHANNEL, ts(now, days=10), msg(ts(now, hours=2), ALICE, "answer"))
    # Thread by Bob where I replied earlier, Alice replies now -> thread_activity.
    fake_slack.add(CHANNEL, msg(ts(now, days=5), BOB, "bob's thread"))
    fake_slack.add_reply(CHANNEL, ts(now, days=5), msg(ts(now, days=4), ME, "my reply"))
    fake_slack.add_reply(CHANNEL, ts(now, days=5), msg(ts(now, hours=1), ALICE, "alice again"))
    # Direct mention in a new top-level message.
    fake_slack.add(CHANNEL, msg(ts(now, minutes=30), ALICE, f"<@{ME}> ping"))
    # My recent message with a reaction from Bob -> reaction.
    fake_slack.add(
        PRIVATE,
        msg(
            ts(now, minutes=20),
            ME,
            "done",
            reactions=[{"name": "tada", "count": 1, "users": [BOB]}],
        ),
    )
    # Noise: my own message, an unrelated message, a reply in a thread I'm not in.
    fake_slack.add(PRIVATE, msg(ts(now, minutes=10), ME, "ignore me"))
    fake_slack.add(PRIVATE, msg(ts(now, minutes=5), ALICE, "unrelated"))
    fake_slack.add(CHANNEL, msg(ts(now, days=2), ALICE, "alice's thread"))
    fake_slack.add_reply(CHANNEL, ts(now, days=2), msg(ts(now, minutes=3), BOB, "bob replies"))

    poller = NotificationPoller(slack, store, rules=RULES, channels=[CHANNEL, PRIVATE])
    found = await poller.poll(since=now - timedelta(days=1), now=now)

    assert [(n.type, n.message["text"]) for n in found] == [
        ("reaction", "done"),
        ("mention", f"<@{ME}> ping"),
        ("thread_activity", "alice again"),
        ("reply_to_me", "answer"),
    ]
    assert found[3].parent is not None and found[3].parent["text"] == "my old question"
    assert await store.get_my_threads(CHANNEL) == {ts(now, days=10), ts(now, days=5)}
    assert await store.get_cursor(CHANNEL) == ts(now, minutes=3)

    # Second poll from the cursor: nothing new, and no replies fetched for quiet threads.
    fake_slack.calls.clear()
    again = await poller.poll(
        since=datetime.fromtimestamp(float(ts(now, minutes=3)), tz=UTC), now=now
    )
    assert again == []
    assert not [c for c in fake_slack.calls if c[0] == "conversations.replies"]


async def test_poller_respects_until(fake_slack: FakeSlack, slack: SlackClient) -> None:
    now = datetime.now(UTC)
    fake_slack.add(CHANNEL, msg(ts(now, hours=5), ALICE, f"<@{ME}> early"))
    fake_slack.add(CHANNEL, msg(ts(now, hours=1), ALICE, f"<@{ME}> late"))
    poller = NotificationPoller(slack, InMemoryStore(), rules=RULES, channels=[CHANNEL])
    found = await poller.poll(
        since=now - timedelta(days=1), until=now - timedelta(hours=2), now=now
    )
    assert [n.message["text"] for n in found] == [f"<@{ME}> early"]
