"""Show work before the brain starts, then edit the same Slack message."""

import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from illo.brain.client import BrainEvent
from illo.gateway import InboundEvent
from illo.gateway.replies import RUN_ERROR, error_text

if TYPE_CHECKING:
    from illo.app import AppContext

from illo.channels.slack.formatting import chunks
from illo.db import one
from illo.ledger import kv

ERROR = RUN_ERROR


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
            if not await kv.get(conn, f"slack:team-brief:{event.payload['channel_id']}:{root}"):
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
        from psycopg.types.json import Jsonb

        from illo.asks import cards

        client, channel = self.ctx.slack_client, event.payload["channel_id"]
        ts = self.working.get(event.external_id)
        outside_draft = None
        async with self.ctx.pool.connection() as conn:
            row = await one(
                conn, "SELECT id, tainted FROM events WHERE channel='slack' AND external_id=%s",
                (event.external_id,),
            )
            tainted = bool(row and row["tainted"])
            if tainted and (event.reply_to or ts):
                await kv.set(
                    conn, f"slack:thread-tainted:{channel}:{event.reply_to or ts}", True
                )
            if row and event.kind != "dm" and event.payload.get("channel_class") != "members":
                outside_draft = await one(
                    conn,
                    """SELECT d.* FROM drafts d
                    JOIN runs r ON r.id = d.run_id
                    WHERE r.event_id = %s
                      AND d.kind = 'github_assign_issue'
                      AND d.state = 'pending'
                      AND d.content ? 'outside_ticket'
                    ORDER BY d.id DESC LIMIT 1""",
                    (row["id"],),
                )
        parts = chunks(text)
        channel_blocks = (
            cards.outside_ticket_channel_blocks(outside_draft, parts[0])
            if outside_draft and len(parts) == 1 and len(parts[0]) <= 2900
            else None
        )

        def delivery_id(index):
            if not event.payload.get("delivery_key"):
                return {}
            from illo.asks.service import message_id

            return {"client_msg_id": message_id("deep", f"{event.payload['delivery_key']}:{index}")}

        try:
            if ts:
                kwargs = {"blocks": channel_blocks} if channel_blocks else {}
                await client.chat_update(channel=channel, ts=ts, text=parts[0], **kwargs)
            else:
                kwargs = {"blocks": channel_blocks} if channel_blocks else {}
                sent = await client.chat_postMessage(
                    channel=channel,
                    text=parts[0],
                    **delivery_id(0),
                    **kwargs,
                    thread_ts=event.reply_to,
                    unfurl_links=False,
                    unfurl_media=False,
                )
                ts = sent["ts"]
                if tainted:
                    async with self.ctx.pool.connection() as conn:
                        await kv.set(
                            conn, f"slack:thread-tainted:{channel}:{event.reply_to or ts}", True
                        )
            if outside_draft and channel_blocks and ts:
                async with self.ctx.pool.connection() as conn:
                    locked = await one(
                        conn, "SELECT content FROM drafts WHERE id=%s FOR UPDATE", (outside_draft["id"],)
                    )
                    if locked and locked["content"].get("outside_ticket"):
                        content = dict(locked["content"])
                        notice = dict(content["outside_ticket"])
                        notice["channel_card"] = {
                            "channel": channel,
                            "ts": ts,
                            "text": parts[0],
                            "digest": cards.digest(channel_blocks),
                        }
                        content["outside_ticket"] = notice
                        await conn.execute(
                            "UPDATE drafts SET content=%s WHERE id=%s",
                            (Jsonb(content), outside_draft["id"]),
                        )
            for index, part in enumerate(parts[1:], 1):
                await client.chat_postMessage(
                    channel=channel,
                    text=part,
                    **delivery_id(index),
                    thread_ts=event.reply_to or ts,
                    unfurl_links=False,
                    unfurl_media=False,
                )
        finally:
            self.working.pop(event.external_id, None)
            self.progress_at.pop(event.external_id, None)

    async def deliver_error(self, event: InboundEvent, *, category="internal") -> None:
        await self.deliver_final(event, error_text(category))
