"""Build gateway events from Socket Mode payloads, never from message instructions."""

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from psycopg.types.json import Jsonb

if TYPE_CHECKING:
    from illo.app import AppContext
    from illo.gateway.principal import Authority

from illo.channels.slack import actions
from illo.channels.slack.access import channel_private, hide_index
from illo.channels.slack.adapter import SlackAdapter
from illo.channels.slack.blocks import message_text
from illo.channels.slack.channels import member_channels
from illo.channels.slack.clips import read_clips
from illo.channels.slack.threads import read_context, read_dm_history
from illo.channels.slack.voice import OpenAITranscriber
from illo.db import one
from illo.gateway import InboundEvent
from illo.ledger import items, kv, people, sensors
from illo.tools._format import data_text

log = logging.getLogger(__name__)
JOIN_NOTICE = "I am here. Mention me to give me a task or to ask a question."
MEMBERS_CONTEXT = (
    "This channel is private to its members. A task that you make here goes to the team list. "
    "Do not copy other text of this channel into a task."
)
CLIP_ERROR = (
    "I could not read your voice clip. Slack gave no transcript. Write the question as text."
)


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
        self.voice_transport = None
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
        if envelope.get("type") == "view_submission":
            from illo.channels.slack.modals import prepare

            response, finish = await prepare(self.ctx, envelope)
            if finish:
                await finish()
            return response
        if envelope.get("type") == "block_actions":
            await actions.dispatch(self.ctx, envelope)
            return
        raw = envelope.get("event", {})
        kind = raw.get("type")
        if kind == "member_joined_channel":
            if raw.get("user") == self.bot_id and raw.get("channel"):
                await self.join_notice(raw["channel"])
            return
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
        configured = self.ctx.workspace.config.slack.channel(channel)
        if not dm and configured.mode == "off":
            return
        if not dm and not configured.listed:
            if (not raw.get("user") or raw.get("bot_id") or raw.get("bot_profile")
                    or raw.get("workflow_id") or raw.get("subtype") == "bot_message"):
                return
            async with self.ctx.pool.connection() as conn:
                if not await people.by_slack_id(conn, raw["user"]):
                    return
        private = False
        if not dm:
            try:
                private = await channel_private(self.ctx, channel, bot_id=self.bot_id)
            except Exception:
                log.error("Slack could not verify channel privacy")
                payload = {"text": message_text(raw), "payload": {
                    "channel_id": channel, "message_ts": ts,
                }}
                for fn in self.ctx.gateway.payload_filters:
                    payload = fn(payload)
                async with self.ctx.pool.connection() as conn:
                    row = await one(conn, """
                        INSERT INTO events (channel,external_id,kind,payload,status)
                        VALUES ('slack',%s,'message',%s,'failed')
                        ON CONFLICT (channel,external_id) DO NOTHING RETURNING id
                    """, (f"{channel}:{ts}", Jsonb(payload)))
                return {"decision": "ignore", "event_id": row["id"] if row else None,
                        "error": "Slack could not verify channel privacy"}
            if private:
                await hide_index(self.ctx, channel)
        thread = raw.get("thread_ts")
        outside_workspace = False
        if raw.get("user_team"):
            if not self.team_id:
                self.team_id = (await self.ctx.slack_client.auth_test())["team_id"]
            outside_workspace = raw["user_team"] != self.team_id
        if outside_workspace and not dm and not configured.listed:
            return
        mentioned = kind == "app_mention" or f"<@{self.bot_id}>" in raw.get("text", "")
        if thread and not dm:
            async with self.ctx.pool.connection() as conn:
                expires = await kv.get(conn, f"slack:team-brief:{channel}:{thread}")
                follows = (
                    datetime.now(UTC) < datetime.fromisoformat(expires) if expires else
                    bool(await kv.get(conn, f"slack:thread:{channel}:{thread}"))
                )
                mentioned = mentioned or follows
        if private and not mentioned:
            # Illo keeps no text of a private channel unless a person speaks to it.
            return {"decision": "ignore"}
        text = message_text(raw)
        event = InboundEvent(
            channel="slack",
            external_id=f"{channel}:{ts}",
            kind="dm" if dm else "message",
            sender=raw.get("user", ""),
            text=text,
            reply_to=thread if dm else thread or ts,
            payload={
                "channel_id": channel,
                "channel_class": "members" if private else "team",
                "outside_workspace": outside_workspace,
                "message_ts": ts,
                "thread_ts": thread,
                "mentioned": mentioned,
                "bot": bool(raw.get("bot_id") or raw.get("bot_profile") or raw.get("workflow_id")),
                "bot_id": raw.get("bot_id"),
                "author_slack_id": raw.get("user"),
                "webhook": raw.get("subtype") == "bot_message",
                "form": bool(raw.get("workflow_id")),
                "files": [
                    {
                        k: f[k]
                        for k in (
                            "id",
                            "mimetype",
                            "size",
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
        if dm and text != raw.get("text", ""):
            # Claims use only what the person typed, before block or thread context.
            event.payload["slack_typed_text"] = raw.get("text", "")
        # Only verified team channels enter the shared index.
        result = await self.ctx.gateway.receive(event)
        if not dm and not private:
            await self.index(
                event.model_copy(update={"text": text}), configured.name
            )
        return result

    async def join_notice(self, channel):
        if self.ctx.workspace.config.slack.channel(channel).mode == "off":
            return
        key = f"slack:join-notice:{channel}"
        try:
            async with self.ctx.pool.connection() as conn:
                await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (key,))
                if await kv.get(conn, key):
                    return
                # Claim before the send. An uncertain Slack response must not send twice.
                await kv.set(conn, key, True)
            await self.ctx.slack_client.chat_postMessage(channel=channel, text=JOIN_NOTICE)
        except Exception:
            async with self.ctx.pool.connection() as conn:
                await sensors.error(conn, key, "Slack could not post the join notice")

    async def index(self, event: InboundEvent, channel_name: str) -> None:
        client = self.ctx.slack_client
        try:
            if await channel_private(self.ctx, event.payload["channel_id"], bot_id=self.bot_id):
                await hide_index(self.ctx, event.payload["channel_id"])
                return
            link = await client.chat_getPermalink(
                channel=event.payload["channel_id"], message_ts=event.payload["message_ts"]
            )
            async with self.ctx.pool.connection() as conn:
                person = await people.by_slack_id(conn, event.sender)
                cached = await kv.get(conn, "slack:member-channels")
                channel_name = (cached or {}).get("channels", {}).get(event.payload["channel_id"], channel_name)
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
        voice = ctx.workspace.config.voice
        fallback = None
        if voice.transcription == "openai":
            fallback = OpenAITranscriber(
                ctx, event, authority, self.bot_id,
                self.adapter.working.get(event.external_id), transport=self.voice_transport,
                slack_timeout=self.clip_timeout, slack_poll=self.clip_poll,
                team_id=self.team_id,
            )
        clip_text = await read_clips(
            self.ctx.slack_client,
            event.payload["files"],
            self.clip_timeout,
            self.clip_poll,
            fallback=fallback,
        )
        payload = dict(event.payload)
        if clip_text is None:
            payload["clip_unreadable"] = True
            return event.model_copy(update={"payload": payload})
        if "slack_typed_text" in payload:
            payload["slack_typed_text"] = "\n".join([
                payload["slack_typed_text"], *clip_text,
            ]).strip()
        return event.model_copy(update={
            "text": "\n".join([event.text, *clip_text]).strip(), "payload": payload,
        })

    async def prepare_context(self, event: InboundEvent) -> None:
        """Add conversation history only after claims pass."""
        limit = self.ctx.workspace.config.run_limits.max_message_chars
        if len(event.text) > limit:
            raise ValueError("The message is too long")
        if event.kind != "dm":
            try:
                await member_channels(self.ctx)
            except ValueError:
                pass
        source = "thread"
        context = []
        if event.payload.get("thread_ts"):
            context = await read_context(
                self.ctx,
                event,
                self.bot_id,
                self.adapter.working.get(event.external_id),
            )
        elif event.kind == "dm":
            source = "dm-history"
            context = await read_dm_history(
                self.ctx, event, self.bot_id,
                self.adapter.working.get(event.external_id), bot_identity=self.bot_identity,
            )
        from illo.channels.slack.tasks import context as task_context

        task = await task_context(self.ctx, event)
        if task:
            context.append(task)
        budget = limit - len(event.text) - len(data_text(source, "")) - len("\n\nRequest:\n")
        if context and budget > 0:
            history = "\n".join(context)[-budget:]
            # Escaping nested markers can expand the text. Keep the final block in budget.
            while history and len(data_text(source, history)) > budget + len(data_text(source, "")):
                history = history[1:]
            if history:
                event.text = data_text(source, history) + "\n\nRequest:\n" + event.text
