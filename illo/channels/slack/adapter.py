"""Show work before the brain starts, then edit the same Slack message."""

import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from illo.brain.client import BrainEvent
from illo.gateway import InboundEvent

if TYPE_CHECKING:
    from illo.app import AppContext

from illo.channels.slack.formatting import chunks
from illo.ledger import kv

ERROR = "I could not finish this. The error is logged. Please try again."


class SlackAdapter:
    def __init__(
        self, ctx: "AppContext", prepare: Callable[[InboundEvent], Awaitable[None]] | None = None
    ):
        self.ctx = ctx
        self.prepare = prepare
        self.working: dict[str, str] = {}
        self.progress_at: dict[str, float] = {}

    async def show_working(self, event: InboundEvent, *, prepare: bool = True) -> None:
        if event.external_id in self.working:
            if prepare and self.prepare:
                await self.prepare(event)
            return
        response = await self.ctx.slack_client.chat_postMessage(
            channel=event.payload["channel_id"],
            text="Looking…",
            thread_ts=event.reply_to,
            unfurl_links=False,
            unfurl_media=False,
        )
        self.working[event.external_id] = response["ts"]
        root = event.reply_to or response["ts"]
        # Persist the thread marker so a restart does not lose a conversation.
        async with self.ctx.pool.connection() as conn:
            await kv.set(conn, f"slack:thread:{event.payload['channel_id']}:{root}", True)
        if prepare and self.prepare:
            await self.prepare(event)

    async def show_progress(self, event: InboundEvent, item: BrainEvent) -> None:
        if item.type != "tool_call" or event.external_id not in self.working:
            return
        now = time.monotonic()
        if now - self.progress_at.get(event.external_id, 0) < 2:
            return
        self.progress_at[event.external_id] = now
        family = (item.name or "").split("_", 1)[0]
        text = {
            "lanes": "Reading the lanes…",
            "github": "Reading GitHub…",
            "search": "Searching…",
        }.get(family, "Looking…")
        await self.ctx.slack_client.chat_update(
            channel=event.payload["channel_id"],
            ts=self.working[event.external_id],
            text=text,
        )

    async def deliver_final(self, event: InboundEvent, text: str) -> None:
        client, channel = self.ctx.slack_client, event.payload["channel_id"]
        ts = self.working.get(event.external_id)
        parts = chunks(text)
        try:
            if ts:
                await client.chat_update(channel=channel, ts=ts, text=parts[0])
            else:
                sent = await client.chat_postMessage(
                    channel=channel,
                    text=parts[0],
                    thread_ts=event.reply_to,
                    unfurl_links=False,
                    unfurl_media=False,
                )
                ts = sent["ts"]
            for part in parts[1:]:
                await client.chat_postMessage(
                    channel=channel,
                    text=part,
                    thread_ts=event.reply_to or ts,
                    unfurl_links=False,
                    unfurl_media=False,
                )
        finally:
            self.working.pop(event.external_id, None)
            self.progress_at.pop(event.external_id, None)

    async def deliver_error(self, event: InboundEvent) -> None:
        await self.deliver_final(event, ERROR)
