"""Keep the Socket Mode connection alive without a public HTTP endpoint."""

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from illo.app import AppContext

from slack_sdk.http_retry.builtin_async_handlers import AsyncRateLimitErrorRetryHandler
from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web.async_client import AsyncWebClient

from illo.channels.slack.intake import SlackIntake
from illo.ledger import kv, sensors

log = logging.getLogger(__name__)


class SlackConnection:
    def __init__(self, ctx: "AppContext"):
        self.ctx = ctx
        self.socket: SocketModeClient | None = None
        self.intake: SlackIntake | None = None
        self.pending: set[asyncio.Task] = set()

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
            if finish:
                task = asyncio.create_task(finish())
                self.pending.add(task)
                task.add_done_callback(self.pending.discard)
            return
        # Acknowledge before database, file, or model work.
        await client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
        if request.type not in {"events_api", "interactive"}:
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

    async def run(self) -> None:
        """The SDK reconnects active sockets. Retry initial and failed connections too."""
        ctx = self.ctx
        try:
            while True:
                try:
                    if self.socket is None:
                        auth = await ctx.slack_client.auth_test()
                        async with ctx.pool.connection() as conn:
                            await kv.set(conn, "slack:bot", {"name": auth.get("user"), "id": auth["user_id"]})
                        if self.intake is None:
                            self.intake = SlackIntake(
                                ctx, auth["user_id"], bot_identity=auth.get("bot_id")
                            )
                        self.socket = SocketModeClient(
                            app_token=ctx.config.slack_app_token.get_secret_value(),
                            web_client=ctx.slack_client,
                            auto_reconnect_enabled=True,
                        )
                        self.socket.socket_mode_request_listeners.append(self.on_request)
                        await self.socket.connect()
                    if not await self.socket.is_connected():
                        await self.report(False)
                        await self.socket.connect()
                    else:
                        await self.report(True)
                except Exception:
                    log.error("Slack could not connect. It will retry.")
                    await self.report(False)
                    if self.socket:
                        await self.socket.close()
                        self.socket = None
                await asyncio.sleep(10)
        finally:
            for task in self.pending:
                task.cancel()
            await asyncio.gather(*self.pending, return_exceptions=True)
            if self.socket:
                await self.socket.close()
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
