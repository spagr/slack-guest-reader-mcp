import httpx
import pytest
import respx
from tests.conftest import ALICE, CHANNEL, FakeSlack, msg

from slack_guest_reader.slack_client import (
    SlackApiError,
    SlackBudgetExceeded,
    SlackClient,
    time_budget,
)

API = "https://slack.com/api"


def make_client(sleeps: list[float]) -> SlackClient:
    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    return SlackClient("xoxb-TEST-TOKEN", max_retries=3, sleep=fake_sleep)


async def test_pagination_follows_cursor(fake_slack: FakeSlack, slack: SlackClient) -> None:
    for i in range(450):
        fake_slack.add(CHANNEL, msg(f"{1700000000 + i}.000000", ALICE, f"m{i}"))
    rows = await slack.history(CHANNEL, oldest="0", limit=350)
    assert len(rows) == 350
    assert rows[0]["text"] == "m449"  # newest first
    calls = [c for c in fake_slack.calls if c[0] == "conversations.history"]
    assert [c[1]["limit"] for c in calls] == ["200", "150"]
    assert calls[1][1]["cursor"] == "200"


async def test_rate_limit_honours_retry_after() -> None:
    sleeps: list[float] = []
    client = make_client(sleeps)
    with respx.mock(base_url=API) as router:
        route = router.post("/users.info")
        route.side_effect = [
            httpx.Response(429, headers={"Retry-After": "3"}),
            httpx.Response(200, json={"ok": True, "user": {"id": "U1"}}),
        ]
        user = await client.user_info("U1")
    assert user == {"id": "U1"}
    assert sleeps == [3.0]


async def test_server_errors_back_off_then_give_up() -> None:
    sleeps: list[float] = []
    client = make_client(sleeps)
    with respx.mock(base_url=API) as router:
        router.post("/auth.test").mock(return_value=httpx.Response(502))
        with pytest.raises(SlackApiError) as exc:
            await client.auth_test()
    assert exc.value.error == "http_502"
    assert len(sleeps) == 3
    assert sleeps == sorted(sleeps)  # exponential growth (jitter aside)


async def test_ok_false_raises_with_error_code() -> None:
    client = make_client([])
    with respx.mock(base_url=API) as router:
        router.post("/conversations.info").mock(
            return_value=httpx.Response(200, json={"ok": False, "error": "channel_not_found"})
        )
        with pytest.raises(SlackApiError) as exc:
            await client.channel_info("C0000000000")
    assert exc.value.error == "channel_not_found"


async def test_time_budget_caps_retries() -> None:
    sleeps: list[float] = []
    client = make_client(sleeps)
    with respx.mock(base_url=API) as router:
        router.post("/auth.test").mock(
            return_value=httpx.Response(429, headers={"Retry-After": "120"})
        )
        with time_budget(5), pytest.raises(SlackBudgetExceeded):
            await client.auth_test()
    assert sleeps == []  # 120 s does not fit into a 5 s budget, so we fail fast


async def test_transport_errors_are_retried() -> None:
    sleeps: list[float] = []
    client = make_client(sleeps)
    with respx.mock(base_url=API) as router:
        router.post("/auth.test").side_effect = [
            httpx.ConnectError("boom"),
            httpx.Response(200, json={"ok": True, "url": "https://t.slack.com/"}),
        ]
        body = await client.auth_test()
    assert body["ok"] is True
    assert len(sleeps) == 1
