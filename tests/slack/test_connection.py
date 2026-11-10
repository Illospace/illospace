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
        assert len(ctx.gateway.claims) == 1  # Reconnect does not add a second claim.
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
