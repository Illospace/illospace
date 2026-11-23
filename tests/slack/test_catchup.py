"""Read missed Slack messages with the recording client."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from illo.channels.slack import catchup, connection
from illo.db import all_rows, one

NOW = 1700000000
ADA_DM = "D_ACME_ADA"
BEN_DM = "D_ACME_BEN"
TEAM = "C_ACME_TEAM"


def post(age, user="U_ACME_BEN", **fields):
    return {"ts": f"{NOW - age}.000000", "user": user, "text": "Give me an update", **fields}


@pytest.fixture
def history(slack_client, monkeypatch):
    messages = {}
    member_channels = slack_client.users_conversations

    async def users_conversations(**kwargs):
        if kwargs["types"] != "im":
            return await member_channels(**kwargs)
        await slack_client._record("users_conversations", **kwargs)
        if kwargs["cursor"] is None:
            return {
                "channels": [{"id": ADA_DM, "user": "U_ACME_ADA"}],
                "response_metadata": {"next_cursor": "next-page"},
            }
        return {
            "channels": [
                {"id": BEN_DM, "user": "U_ACME_BEN"},
                {"id": "D_ACME_GUEST", "user": "U_ACME_GUEST"},
            ]
        }

    async def conversations_history(**kwargs):
        await slack_client._record("conversations_history", **kwargs)
        # Intake reads DM context again without the lower time bound.
        if "oldest" not in kwargs:
            return {"messages": []}
        return {"messages": messages.get(kwargs["channel"], [])}

    monkeypatch.setattr(slack_client, "users_conversations", users_conversations)
    monkeypatch.setattr(slack_client, "conversations_history", conversations_history)
    monkeypatch.setattr(catchup, "time", SimpleNamespace(time=lambda: NOW))
    return messages


def calls(client, method):
    return [call for call in client.calls if call["method"] == method]


def notices(client):
    return [
        call["text"] for call in calls(client, "chat_postMessage") if call["channel"] == "D_EXAMPLE"
    ]


async def test_recovers_dms_and_channel_mentions_in_time_order(
    app,
    intake,
    slack_client,
    fake_brain,
    history,
):
    history[ADA_DM] = [post(700, "U_ACME_ADA", text="Second request")]
    history[BEN_DM] = [post(900, text="First request")]
    history[TEAM] = [post(400, text="<@U_ACME_ILLO> Third request")]
    ctx = app.state.ctx

    result = await catchup.catch_up(ctx, intake, NOW - 900, NOW)

    assert result.missed == result.recent == result.answered == 3
    assert result.too_old == 0 and not result.failed
    assert [call["channel"] for call in calls(slack_client, "chat_postMessage")] == [
        BEN_DM,
        ADA_DM,
        TEAM,
        "D_EXAMPLE",
    ]
    assert [req.user_message for req in fake_brain.requests] == [
        "First request",
        "Second request",
        "<@U_ACME_ILLO> Third request",
    ]
    last_edits = {call["channel"]: call["text"] for call in calls(slack_client, "chat_update")}
    assert last_edits == dict.fromkeys((BEN_DM, ADA_DM, TEAM), "The lane is working.")
    async with ctx.pool.connection() as conn:
        rows = await all_rows(conn, "SELECT external_id, status FROM events ORDER BY id")
        assert rows == [
            {"external_id": f"{channel}:{history[channel][0]['ts']}", "status": "handled"}
            for channel in (BEN_DM, ADA_DM, TEAM)
        ]
        assert (await one(conn, "SELECT count(*) FROM runs"))["count"] == 3
    assert [call["users"] for call in calls(slack_client, "conversations_open")] == ["U_ACME_ADA"]
    (notice,) = notices(slack_client)
    assert "2023-11-14 21:58:20 to 2023-11-14 22:13:20 (UTC) (0 h 15 min)" in notice
    assert "3 missed messages" in notice and "It answered 3" in notice
    assert "A button press" in notice
    assert not any(
        call["channel"] == "D_ACME_GUEST" for call in calls(slack_client, "conversations_history")
    )


async def test_skips_a_message_that_arrived(
    app, intake, message, fake_brain, slack_client, history
):
    original = post(90, "U_ACME_ADA")
    await intake.receive(message(**original))
    history[ADA_DM] = [original]
    ctx = app.state.ctx
    receive = AsyncMock(wraps=intake.receive)
    intake.receive = receive

    result = await catchup.catch_up(ctx, intake, NOW - 120, NOW)

    assert result.missed == result.answered == 0
    receive.assert_not_awaited()
    assert len(fake_brain.requests) == 1
    assert len(calls(slack_client, "chat_postMessage")) == 1
    async with ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT count(*) FROM events"))["count"] == 1
        assert (await one(conn, "SELECT count(*) FROM runs"))["count"] == 1


async def test_notifies_the_author_of_an_old_message_once(app, intake, slack_client, history):
    old = post(90000)
    history[BEN_DM] = [old]
    history[TEAM] = [post(91000)]  # An old message without a mention needs no notice.
    ctx = app.state.ctx

    result = await catchup.catch_up(ctx, intake, NOW - 92000, NOW)

    assert result.missed == result.too_old == 1 and result.recent == result.answered == 0
    person, owner = notices(slack_client)
    assert "It missed 1 message from you." in person
    assert "older than 24 hours" in person and "A button press" in person
    assert "1 was too old" in owner and "Illo sent a DM to each sender" in owner
    async with ctx.pool.connection() as conn:
        (row,) = await all_rows(conn, "SELECT external_id, kind, status, payload FROM events")
        assert row == {
            "external_id": f"{BEN_DM}:{old['ts']}",
            "kind": "dm",
            "status": "ignored",
            "payload": {"payload": {"channel_id": BEN_DM, "message_ts": old["ts"]}, "missed": True},
        }
        assert (await one(conn, "SELECT count(*) FROM runs"))["count"] == 0
        assert (await one(conn, "SELECT count(*) FROM items WHERE kind='slack'"))["count"] == 0

    again = await catchup.catch_up(ctx, intake, NOW - 92000, NOW)

    assert again.missed == 0
    assert [call["users"] for call in calls(slack_client, "conversations_open")] == [
        "U_ACME_BEN",
        "U_ACME_ADA",
        "U_ACME_ADA",
    ]


async def test_groups_old_dm_and_channel_messages_for_each_person(
    app, intake, slack_client, history
):
    history[BEN_DM] = [post(87000), post(88000, user="U_ACME_ILLO")]
    history[TEAM] = [
        post(89000, text="<@U_ACME_ILLO> Give me an update"),
        post(90000, text="<@U_ACME_ILLO> Outside message", team="T_ACME_GUEST"),
    ]

    result = await catchup.catch_up(app.state.ctx, intake, NOW - 91000, NOW)

    assert result.too_old == 2
    person, _ = notices(slack_client)
    assert "It missed 2 messages from you." in person


async def test_short_gap_sends_no_owner_notice(app, intake, slack_client, history):
    history[BEN_DM] = [post(75)]

    result = await catchup.catch_up(app.state.ctx, intake, NOW - 120, NOW)

    assert result.missed == result.answered == 1
    assert not notices(slack_client)


async def test_reads_thread_replies_and_keeps_the_conversation_limit(
    app,
    intake,
    slack_client,
    history,
    monkeypatch,
):
    oldest = NOW - catchup.MAX_LOOKBACK_S
    thread = post(500, user="U_ACME_ILLO", latest_reply=post(80)["ts"])
    reply = post(110, thread_ts=thread["ts"])
    other_reply = post(80, thread_ts=thread["ts"])
    history[BEN_DM] = [thread, post(catchup.MAX_LOOKBACK_S + 1)]
    read_replies = slack_client.conversations_replies
    requests = []

    async def conversations_replies(**kwargs):
        if "oldest" not in kwargs:
            return await read_replies(**kwargs)
        requests.append(kwargs)
        if len(requests) == 1:
            # Slack can give the next page by time when it has no cursor.
            return {"messages": [thread, reply], "has_more": True}
        return {"messages": [other_reply, post(70, thread_ts=thread["ts"])]}

    monkeypatch.setattr(slack_client, "conversations_replies", conversations_replies)
    monkeypatch.setattr(catchup, "MAX_MESSAGES", 3)
    receive = AsyncMock(return_value=None)
    monkeypatch.setattr(intake, "receive", receive)

    result = await catchup.catch_up(app.state.ctx, intake, oldest - 100, NOW)

    assert result.recent == 2 and not result.failed
    assert [call.args[0]["event"]["ts"] for call in receive.await_args_list] == [
        reply["ts"],
        other_reply["ts"],
    ]
    assert [req["oldest"] for req in requests] == [f"{oldest:.6f}", f"{NOW - 110}.000001"]
    assert [req["limit"] for req in requests] == [2, 1]
    assert all(req["ts"] == thread["ts"] and req["latest"] == f"{NOW:.6f}" for req in requests)


async def test_failed_reads_do_not_stop_the_other_conversations(
    app,
    intake,
    slack_client,
    history,
    monkeypatch,
):
    history[TEAM] = [post(90, text="<@U_ACME_ILLO> Give me an update")]
    read_history = slack_client.conversations_history
    list_conversations = slack_client.users_conversations

    async def conversations_history(**kwargs):
        if kwargs["channel"] == ADA_DM:
            raise RuntimeError("Acme private request")
        return await read_history(**kwargs)

    async def users_conversations(**kwargs):
        if kwargs["types"] != "im":
            raise RuntimeError("Acme private request")
        return await list_conversations(**kwargs)

    monkeypatch.setattr(slack_client, "conversations_history", conversations_history)
    monkeypatch.setattr(slack_client, "users_conversations", users_conversations)
    result = await catchup.catch_up(app.state.ctx, intake, NOW - 700, NOW)

    assert result.missed == result.answered == 1
    assert result.unread_conversations == result.discovery_failures == 1 and result.failed
    (owner,) = notices(slack_client)
    assert "1 conversation." in owner and "complete list of conversations" in owner
    assert "Acme private request" not in owner


@pytest.mark.parametrize(("gap", "greet"), [
    (120, True), (120, False), (60, True), (30, True), (None, True),
])
async def test_connection_uses_the_persisted_last_good_time(
    app, slack_client, monkeypatch, gap, greet
):
    ctx = app.state.ctx
    ctx.config.slack_app_token = SecretStr("invented-app-token")
    slack_client.session = None
    if gap is not None:
        async with ctx.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO sensors (key,last_ok_at) VALUES ('slack',to_timestamp(%s))",
                (NOW - gap,),
            )
    asleep = asyncio.Event()

    class Socket:
        def __init__(self, **kwargs):
            self.socket_mode_request_listeners = []
            self.on_message_listeners = []

        async def connect(self):
            # Without the greeting, Slack sends no events, so the gap stays open.
            for listener in self.on_message_listeners if greet else []:
                await listener(SimpleNamespace(data='{"type": "hello"}'))

        async def session_id(self):
            return "s_acme"

        async def is_connected(self):
            return True

        async def close(self):
            pass

    async def pause(delay):
        assert delay == 10
        asleep.set()
        await asyncio.Event().wait()

    recover = AsyncMock(return_value=catchup.CatchupResult())
    monkeypatch.setattr(connection, "catch_up", recover)
    monkeypatch.setattr(connection, "SocketModeClient", Socket)
    monkeypatch.setattr(
        connection, "time", SimpleNamespace(time=lambda: NOW, monotonic=time.monotonic)
    )
    monkeypatch.setattr(
        connection,
        "asyncio",
        SimpleNamespace(
            sleep=pause,
            gather=asyncio.gather,
            create_task=asyncio.create_task,
        ),
    )
    slack = connection.SlackConnection(ctx)
    task = asyncio.create_task(slack.run())
    try:
        await asyncio.wait_for(asleep.wait(), 1)
        await asyncio.gather(*slack.pending)
        async with ctx.pool.connection() as conn:
            sensor = await one(conn, "SELECT * FROM sensors WHERE key='slack:catch-up'")
        if gap == 120 and greet:
            recover.assert_awaited_once_with(ctx, slack.intake, NOW - gap, NOW)
            assert sensor["last_ok_at"] and not sensor["last_error_at"]
        else:
            recover.assert_not_awaited()
            assert sensor is None
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_connection_records_a_failed_notice(
    app, intake, slack_client, history, monkeypatch, caplog
):
    async def fail(**kwargs):
        raise RuntimeError("Acme private notice")

    monkeypatch.setattr(slack_client, "conversations_open", fail)
    ctx = app.state.ctx
    slack = connection.SlackConnection(ctx)
    slack.intake = intake

    await slack.recover(NOW - 700, NOW)

    async with ctx.pool.connection() as conn:
        sensor = await one(conn, "SELECT * FROM sensors WHERE key='slack:catch-up'")
    assert sensor["last_error_at"] and not sensor["last_ok_at"]
    assert sensor["detail"] == "Slack could not finish the catch-up"
    assert sensor["detail"] in caplog.text and "Acme private notice" not in caplog.text
