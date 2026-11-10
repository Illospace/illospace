"""Build gateway events from Socket Mode payloads, never from message instructions."""

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from illo.app import AppContext
    from illo.gateway.principal import Authority

from illo.channels.slack import actions
from illo.channels.slack.adapter import SlackAdapter
from illo.channels.slack.clips import read_clips
from illo.channels.slack.threads import read_context
from illo.gateway import InboundEvent
from illo.ledger import items, kv, people

log = logging.getLogger(__name__)
CLIP_ERROR = "I could not read this clip. Please send it as text."


class SlackIntake:
    def __init__(
        self,
        ctx: "AppContext",
        bot_id: str,
        *,
        bot_identity: str | None = None,
        clip_timeout: float = 15,
        clip_poll: float = 1,
    ):
        self.ctx, self.bot_id = ctx, bot_id
        self.bot_identity = bot_identity
        self.team_id: str | None = None
        self.clip_timeout, self.clip_poll = clip_timeout, clip_poll
        self.adapter = SlackAdapter(ctx, self.prepare_context)
        ctx.gateway.register("slack", self.adapter)
        ctx.gateway.register_preparer(self.prepare)
        ctx.gateway.register_claim(self.claim_unreadable_clip)

    async def claim_unreadable_clip(
        self, ctx: "AppContext", event: InboundEvent, authority: "Authority"
    ) -> str | None:
        if event.channel == "slack" and event.payload.get("clip_unreadable"):
            return CLIP_ERROR
        return None

    async def receive(self, envelope: dict) -> dict | None:
        if envelope.get("type") == "block_actions":
            await actions.dispatch(self.ctx, envelope)
            return
        raw = envelope.get("event", {})
        kind = raw.get("type")
        if kind not in {"message", "app_mention"}:
            return
        if (
            raw.get("user") == self.bot_id
            or self.bot_identity
            and raw.get("bot_id") == self.bot_identity
            or raw.get("subtype")
            not in {
                None,
                "bot_message",
                "file_share",
                "slack_video",
                "thread_broadcast",
            }
        ):
            return
        channel, ts = raw.get("channel"), raw.get("ts")
        if not channel or not ts:
            return
        dm = raw.get("channel_type") == "im" or channel.startswith("D")
        configured = next(
            (
                c
                for c in self.ctx.workspace.config.slack.channels
                if c.id == channel and c.mode != "off"
            ),
            None,
        )
        if not dm and not configured:
            return
        thread = raw.get("thread_ts")
        outside_workspace = False
        if raw.get("user_team"):
            if not self.team_id:
                self.team_id = (await self.ctx.slack_client.auth_test())["team_id"]
            outside_workspace = raw["user_team"] != self.team_id
        mentioned = kind == "app_mention" or f"<@{self.bot_id}>" in raw.get("text", "")
        if thread and not dm:
            async with self.ctx.pool.connection() as conn:
                mentioned = mentioned or bool(
                    await kv.get(conn, f"slack:thread:{channel}:{thread}")
                )
        event = InboundEvent(
            channel="slack",
            external_id=f"{channel}:{ts}",
            kind="dm" if dm else "message",
            sender=raw.get("user", ""),
            text=raw.get("text", ""),
            reply_to=thread if dm else thread or ts,
            payload={
                "channel_id": channel,
                "outside_workspace": outside_workspace,
                "message_ts": ts,
                "thread_ts": thread,
                "mentioned": mentioned,
                "bot": bool(raw.get("bot_id") or raw.get("bot_profile") or raw.get("workflow_id")),
                "bot_id": raw.get("bot_id"),
                "webhook": raw.get("subtype") == "bot_message",
                "form": bool(raw.get("workflow_id")),
                "files": [
                    {
                        k: f[k]
                        for k in (
                            "id",
                            "mimetype",
                            "is_audio",
                            "subtype",
                            "transcript",
                            "transcription",
                        )
                        if k in f
                    }
                    for f in raw.get("files", [])
                ],
                "from_automation": bool(
                    raw.get("bot_id")
                    or raw.get("bot_profile")
                    or raw.get("workflow_id")
                    or raw.get("subtype") == "bot_message"
                ),
            },
        )
        # Search sees only configured team channels. Never index a private DM.
        result = await self.ctx.gateway.receive(event)
        if configured and not dm:
            await self.index(
                event.model_copy(update={"text": raw.get("text", "")}), configured.name
            )
        return result

    async def index(self, event: InboundEvent, channel_name: str) -> None:
        client = self.ctx.slack_client
        try:
            link = await client.chat_getPermalink(
                channel=event.payload["channel_id"], message_ts=event.payload["message_ts"]
            )
            async with self.ctx.pool.connection() as conn:
                person = await people.by_slack_id(conn, event.sender)
                await items.upsert(
                    conn,
                    source_ref=f"slack:{event.external_id}",
                    kind="slack",
                    title=f"#{channel_name} — {person['name'] if person else event.sender or 'Automation'}",
                    body=event.text,
                    links=[link["permalink"]],
                    visibility="team",
                    source_at=datetime.fromtimestamp(float(event.payload["message_ts"]), UTC),
                )
        except Exception:
            log.error("Slack could not index a channel message")

    async def prepare(self, ctx, event: InboundEvent, authority) -> InboundEvent | None:
        """Read clips after verification and before any business claim."""
        if event.channel != "slack" or not event.payload.get("files"):
            return None
        await self.adapter.show_working(event, prepare=False)
        clip_text = await read_clips(
            self.ctx.slack_client,
            event.payload["files"],
            self.clip_timeout,
            self.clip_poll,
        )
        payload = dict(event.payload)
        if clip_text is None:
            payload["clip_unreadable"] = True
            return event.model_copy(update={"payload": payload})
        return event.model_copy(update={"text": "\n".join([event.text, *clip_text]).strip()})

    async def prepare_context(self, event: InboundEvent) -> None:
        """Add thread history only after claims pass."""
        limit = self.ctx.workspace.config.run_limits.max_message_chars
        if len(event.text) > limit:
            raise ValueError("The message is too long")
        if event.payload.get("thread_ts"):
            context = await read_context(
                self.ctx,
                event,
                self.bot_id,
                self.adapter.working.get(event.external_id),
            )
            budget = limit - len(event.text) - 50
            if context and budget > 0:
                event.text = (
                    "Thread context (data):\n"
                    + "\n".join(context)[-budget:]
                    + "\n\nRequest:\n"
                    + event.text
                )
