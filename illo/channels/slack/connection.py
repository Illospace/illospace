"""Keep the Socket Mode connection alive without a public HTTP endpoint."""

import asyncio
import json
import logging
import time
from asyncio import Lock
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from illo.app import AppContext

from slack_sdk.http_retry.builtin_async_handlers import AsyncRateLimitErrorRetryHandler
from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web.async_client import AsyncWebClient

from illo.channels.slack.catchup import MIN_GAP_S, catch_up
from illo.channels.slack.intake import SlackIntake
from illo.db import one
from illo.ledger import kv, sensors

log = logging.getLogger(__name__)

# Slack greets a live connection in the first seconds.
HELLO_WAIT_S = 30
# Slack replaces a connection after the time that its greeting gives, about 5 hours.
SESSION_DEFAULT_S = 6 * 3600
SESSION_MARGIN_S = 600
# The SDK reconnects a closed socket in some seconds. Give it this number of checks first.
MISSES_BEFORE_RESET = 3
# The time of the last check that had a greeting. A restart reads the gap start here.
LAST_GREETED_KEY = "slack:last-greeted"


class SlackConnection:
    def __init__(self, ctx: "AppContext"):
        self.ctx = ctx
        self.socket: SocketModeClient | None = None
        self.intake: SlackIntake | None = None
        self.pending: set[asyncio.Task] = set()
        self.catch_up_lock = Lock()
        self.session: str | None = None
        self.session_at = 0.0
        self.greeted: str | None = None
        # The SDK names a session by the hash of its socket object. Keep the greeted
        # object, so that Python cannot give its name to a new socket.
        self.greeted_socket = None
        self.session_limit = float(SESSION_DEFAULT_S)
        self.misses = 0
        self.stopping = False

    async def on_message(self, message) -> None:
        """Record the greeting of Slack and the planned time of the connection."""
        try:
            data = json.loads(message.data)
        except (TypeError, ValueError):
            return
        if not isinstance(data, dict) or data.get("type") != "hello":
            return
        self.greeted_socket = getattr(self.socket, "current_session", None)
        self.greeted = await self.socket.session_id()
        limit = (data.get("debug_info") or {}).get("approximate_connection_time")
        valid = isinstance(limit, int | float) and not isinstance(limit, bool) and limit > 0
        self.session_limit = float(limit) if valid else float(SESSION_DEFAULT_S)

    async def alive(self, now: float) -> bool:
        """Say if this connection can receive events. An open socket is not sufficient.

        Slack can accept a socket and send it no events. Such a socket answers each ping,
        so the SDK sees no fault. It has no greeting, and Slack never replaces it.
        """
        session = await self.socket.session_id()
        if session != self.session:
            self.session, self.session_at = session, now
        if not await self.socket.is_connected():
            return False
        age = now - self.session_at
        if self.greeted != session and age > HELLO_WAIT_S:
            return False
        return age <= self.session_limit + SESSION_MARGIN_S

    async def report(self, healthy: bool) -> None:
        async with self.ctx.pool.connection() as conn:
            if healthy:
                await sensors.ok(conn, "slack", expected_every_s=10)
            else:
                await sensors.error(conn, "slack", "The Slack connection is unavailable", expected_every_s=10)

    async def on_request(self, client: SocketModeClient, request: SocketModeRequest) -> None:
        if request.type == "interactive" and request.payload.get("type") == "view_submission":
            from illo.channels.slack.modals import prepare

            response, finish = await prepare(self.ctx, request.payload)
            await client.send_socket_mode_response(SocketModeResponse(
                envelope_id=request.envelope_id, payload=response))
            if finish and not self.stopping:
                task = asyncio.create_task(finish())
                self.pending.add(task)
                task.add_done_callback(self.pending.discard)
            return
        # Acknowledge before database, file, or model work.
        await client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
        if request.type not in {"events_api", "interactive"} or self.stopping:
            return
        task = asyncio.create_task(self.handle(request.payload))
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)

    async def handle(self, payload: dict) -> None:
        try:
            await self.intake.receive(payload)
        except Exception:
            log.error("Slack could not handle an event")
            await self.report(False)

    async def recover(self, gap_start: float, gap_end: float) -> None:
        async with self.catch_up_lock:
            try:
                result = await catch_up(self.ctx, self.intake, gap_start, gap_end)
                failed = result.failed
            except Exception:
                failed = True
            detail = "Slack could not finish the catch-up"
            if failed:
                log.error(detail)
            try:
                async with self.ctx.pool.connection() as conn:
                    if failed:
                        await sensors.error(conn, "slack:catch-up", detail)
                    else:
                        await sensors.ok(conn, "slack:catch-up")
            except Exception:
                log.error("Slack could not record the catch-up result")

    async def run(self) -> None:
        """The SDK reconnects active sockets. Start again with a new client when it does not."""
        ctx = self.ctx
        last_ok = None
        loaded_last_ok = False
        try:
            while True:
                try:
                    if not loaded_last_ok:
                        # The sensor also records a socket with no greeting. Use the
                        # time of the last greeted check, and the sensor only before it exists.
                        async with ctx.pool.connection() as conn:
                            last_ok = await kv.get(conn, LAST_GREETED_KEY)
                            if last_ok is None:
                                row = await one(
                                    conn, "SELECT extract(epoch from last_ok_at) AS at "
                                    "FROM sensors WHERE key='slack'",
                                )
                                last_ok = row["at"] if row else None
                        last_ok = float(last_ok) if last_ok is not None else None
                        loaded_last_ok = True
                    if self.socket is None:
                        auth = await ctx.slack_client.auth_test()
                        async with ctx.pool.connection() as conn:
                            await kv.set(conn, "slack:bot", {"name": auth.get("user"), "id": auth["user_id"]})
                            await kv.set(conn, "slack:team", auth["team_id"])
                        if self.intake is None:
                            self.intake = SlackIntake(
                                ctx, auth["user_id"], bot_identity=auth.get("bot_id")
                            )
                        self.intake.team_id = auth["team_id"]
                        self.socket = SocketModeClient(
                            app_token=ctx.config.slack_app_token.get_secret_value(),
                            web_client=ctx.slack_client,
                            auto_reconnect_enabled=True,
                        )
                        self.socket.socket_mode_request_listeners.append(self.on_request)
                        self.socket.on_message_listeners.append(self.on_message)
                        # A new client can get the session name of an old one. Start its record again.
                        self.session, self.session_at, self.greeted = None, 0.0, None
                        self.greeted_socket = None
                        self.session_limit, self.misses = float(SESSION_DEFAULT_S), 0
                        await self.socket.connect()
                    if await self.alive(time.monotonic()):
                        self.misses = 0
                        # Only a greeting proves that events arrive. Before it, keep the
                        # old time, so a socket that never gets events stays in the gap.
                        if self.greeted == self.session:
                            now = time.time()
                            if (not self.stopping and last_ok is not None
                                    and now - last_ok > MIN_GAP_S):
                                task = asyncio.create_task(self.recover(last_ok, now))
                                self.pending.add(task)
                                task.add_done_callback(self.pending.discard)
                            last_ok = now
                            async with ctx.pool.connection() as conn:
                                await kv.set(conn, LAST_GREETED_KEY, now)
                        await self.report(True)
                    else:
                        self.misses += 1
                        await self.report(False)
                        if self.misses >= MISSES_BEFORE_RESET:
                            # Do not call `connect` on this client again: it uses the old
                            # address, and Slack sends no events to a socket on an old address.
                            log.error("Slack sends no events on this connection. Illo opens a new one.")
                            await self.socket.close()
                            self.socket = None
                except Exception:
                    log.error("Slack could not connect. It will retry.")
                    await self.report(False)
                    if self.socket:
                        await self.socket.close()
                        self.socket = None
                await asyncio.sleep(10)
        finally:
            # Close the socket first, so no new event starts work during shutdown.
            self.stopping = True
            if self.socket:
                await self.socket.close()
            for task in self.pending:
                task.cancel()
            await asyncio.gather(*self.pending, return_exceptions=True)
            if ctx.slack_client.session:
                await ctx.slack_client.session.close()


async def setup(ctx: "AppContext") -> None:
    if not ctx.config.slack_app_token or not ctx.config.slack_bot_token:
        log.info("Slack is off. Set ILLO_SLACK_APP_TOKEN and ILLO_SLACK_BOT_TOKEN to connect.")
        return
    ctx.slack_client = AsyncWebClient(token=ctx.config.slack_bot_token.get_secret_value())
    ctx.slack_client.retry_handlers.append(AsyncRateLimitErrorRetryHandler(max_retry_count=1))
    connection = SlackConnection(ctx)
    # The scheduler cancels and awaits this task before the pool closes.
    ctx.scheduler.tasks.append(asyncio.create_task(connection.run()))
