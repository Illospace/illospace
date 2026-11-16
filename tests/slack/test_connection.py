"""Socket tests use a fake transport. They never connect to Slack."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from illo.channels.slack.connection import SlackConnection, setup
from illo.db import one


async def test_no_tokens_do_not_create_client(app, caplog):
    ctx = app.state.ctx
    ctx.slack_client = None
    count = len(ctx.scheduler.tasks)
    with caplog.at_level("INFO"):
        await setup(ctx)
    assert ctx.slack_client is None
    assert len(ctx.scheduler.tasks) == count
    assert "Slack is off" in caplog.text


async def test_connect_retry_sensor_and_shutdown(app, slack_client, monkeypatch):
    ctx = app.state.ctx
    ctx.config.slack_app_token = SecretStr("invented-app-token")
    attempts, sockets = [], []
    sleeping = asyncio.Queue()
    resume = asyncio.Queue()

    class Socket:
        def __init__(self, **kwargs):
            assert kwargs["web_client"] is slack_client
            assert kwargs["auto_reconnect_enabled"]
            self.socket_mode_request_listeners = []
            self.on_message_listeners = []
            self.closed = False
            self.connected = False
            sockets.append(self)

        async def connect(self):
            attempts.append("connect")
            if len(attempts) == 1:
                raise RuntimeError("A private transport failure")
            self.connected = True

        async def is_connected(self):
            return self.connected

        async def session_id(self):
            return f"s_{id(self)}"

        async def close(self):
            self.closed = True

    async def pause(delay):
        assert delay == 10
        await sleeping.put(True)
        await resume.get()

    monkeypatch.setattr("illo.channels.slack.connection.SocketModeClient", Socket)
    # Patch the module reference, not asyncio.sleep used by the rest of the app.
    from types import SimpleNamespace

    monkeypatch.setattr(
        "illo.channels.slack.connection.asyncio",
        SimpleNamespace(sleep=pause, gather=asyncio.gather),
    )
    slack_client.session = None
    connection = SlackConnection(ctx)
    task = asyncio.create_task(connection.run())
    try:
        await asyncio.wait_for(sleeping.get(), 1)
        async with ctx.pool.connection() as conn:
            sensor = await one(conn, "SELECT * FROM sensors WHERE key='slack'")
            assert sensor["last_error_at"] and not sensor["last_ok_at"]
            assert "private" not in sensor["detail"]
        assert sockets[0].closed
        await resume.put(True)
        await asyncio.wait_for(sleeping.get(), 1)
        async with ctx.pool.connection() as conn:
            sensor = await one(conn, "SELECT * FROM sensors WHERE key='slack'")
            assert sensor["last_ok_at"] > sensor["last_error_at"]
        slack_claims = [c for c in ctx.gateway.claims if "claim_unreadable_clip" in repr(c)]
        assert len(slack_claims) == 1  # Reconnect does not add a second claim.
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert sockets[-1].closed


async def test_configured_setup_uses_scheduler_shutdown(app, monkeypatch):
    ctx = app.state.ctx
    ctx.config.slack_app_token = SecretStr("invented-app-token")
    ctx.config.slack_bot_token = SecretStr("invented-bot-token")
    entered = asyncio.Event()
    closed = asyncio.Event()

    async def run(self):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            closed.set()

    monkeypatch.setattr(SlackConnection, "run", run)
    await setup(ctx)
    await asyncio.wait_for(entered.wait(), 1)
    await ctx.scheduler.close()
    assert closed.is_set()


async def test_bad_envelope_has_no_background_work(app):
    client = AsyncMock()
    connection = SlackConnection(app.state.ctx)
    from types import SimpleNamespace

    await connection.on_request(client, SimpleNamespace(type="disconnect", envelope_id="ignored"))
    assert not connection.pending
    client.send_socket_mode_response.assert_awaited_once()


class FakeSocket:
    def __init__(self, name="s_1"):
        self.name, self.connected = name, True

    async def session_id(self):
        return self.name

    async def is_connected(self):
        return self.connected


def hello(seconds=18000):
    from types import SimpleNamespace

    return SimpleNamespace(data=f'{{"type":"hello","debug_info":{{"approximate_connection_time":{seconds}}}}}')


async def test_open_socket_with_no_greeting_is_not_alive(app):
    connection = SlackConnection(app.state.ctx)
    connection.socket = FakeSocket()
    assert await connection.alive(1000)
    # The socket answers each ping, but Slack did not greet it. It gets no events.
    assert await connection.alive(1030)
    assert not await connection.alive(1031)


async def test_greeted_socket_is_alive_until_slack_must_replace_it(app):
    connection = SlackConnection(app.state.ctx)
    connection.socket = FakeSocket()
    assert await connection.alive(1000)
    await connection.on_message(hello(18000))
    assert await connection.alive(1000 + 18000 + 600)
    assert not await connection.alive(1000 + 18000 + 601)
    # A new socket starts a new period and needs its own greeting.
    connection.socket = FakeSocket("s_2")
    assert await connection.alive(30000)
    assert not await connection.alive(30031)
    await connection.on_message(hello())
    assert await connection.alive(30031)


async def test_other_messages_and_bad_values_are_not_a_greeting(app):
    from types import SimpleNamespace

    connection = SlackConnection(app.state.ctx)
    connection.socket = FakeSocket()
    for data in ("not json", None, '{"type":"events_api"}', '["hello"]'):
        await connection.on_message(SimpleNamespace(data=data))
    assert connection.greeted is None
    await connection.on_message(SimpleNamespace(data='{"type":"hello","debug_info":{"approximate_connection_time":"soon"}}'))
    assert connection.greeted == "s_1" and connection.session_limit == 6 * 3600


async def test_dead_connection_gets_a_new_client_and_no_second_connect(app, slack_client, monkeypatch):
    ctx = app.state.ctx
    ctx.config.slack_app_token = SecretStr("invented-app-token")
    sockets, connects = [], []
    sleeping, resume = asyncio.Queue(), asyncio.Queue()

    class Socket:
        def __init__(self, **kwargs):
            self.socket_mode_request_listeners, self.on_message_listeners = [], []
            self.closed = False
            sockets.append(self)

        async def connect(self):
            connects.append(self)

        async def is_connected(self):
            # The first socket closes. The SDK does not repair it.
            return self is not sockets[0]

        async def session_id(self):
            # Each client gives the same name, as a reused object hash does.
            return "s_same"

        async def close(self):
            self.closed = True

    async def pause(delay):
        await sleeping.put(True)
        await resume.get()

    from types import SimpleNamespace

    monkeypatch.setattr("illo.channels.slack.connection.SocketModeClient", Socket)
    monkeypatch.setattr("illo.channels.slack.connection.asyncio", SimpleNamespace(sleep=pause, gather=asyncio.gather))
    slack_client.session = None
    connection = SlackConnection(ctx)
    task = asyncio.create_task(connection.run())
    try:
        for _ in range(3):
            await asyncio.wait_for(sleeping.get(), 1)
            await resume.put(True)
        await asyncio.wait_for(sleeping.get(), 1)
        # Three failed checks close the client. Illo never calls `connect` on it a second time.
        assert sockets[0].closed and connects == sockets[:len(connects)]
        assert len(sockets) == 2 and connects.count(sockets[0]) == 1
        # The new client has its own record, although its session name is the same.
        assert connection.misses == 0 and connection.greeted is None
        async with ctx.pool.connection() as conn:
            sensor = await one(conn, "SELECT * FROM sensors WHERE key='slack'")
            assert sensor["last_ok_at"] > sensor["last_error_at"]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_shutdown_starts_no_new_work(app):
    from types import SimpleNamespace

    client = AsyncMock()
    connection = SlackConnection(app.state.ctx)
    connection.stopping = True
    await connection.on_request(client, SimpleNamespace(type="events_api", envelope_id="e", payload={}))
    assert not connection.pending
    client.send_socket_mode_response.assert_awaited_once()
