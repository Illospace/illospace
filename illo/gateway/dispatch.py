"""Receive each event once, resolve its principal, and deliver its answer."""

import asyncio
import logging
from dataclasses import replace

from psycopg.types.json import Jsonb

from illo.brain.run_manager import RunManager
from illo.db import one
from illo.gateway.events import ChannelAdapter, InboundEvent, record_event
from illo.gateway.principal import resolve
from illo.gateway.replies import error_text, failure_category

log = logging.getLogger(__name__)


class ClaimRetry(RuntimeError):
    """A verified claim needs the same event again."""


class Gateway:
    def __init__(self, ctx):
        self.ctx = ctx
        self.run_manager = RunManager(ctx)
        self.adapters: dict[str, ChannelAdapter] = {}
        self.direct_handlers = {}
        self.claims = []
        self.preparers = []
        self.payload_filters = []
        self.finalizers = []

    def register_finalizer(self, fn) -> None:
        """Run a code notice after the channel delivers the answer."""
        self.finalizers.append(fn)

    def register(self, channel: str, adapter: ChannelAdapter) -> None:
        """A channel's setup hook registers its delivery adapter."""
        self.adapters[channel] = adapter

    def register_direct(self, channel: str, kind: str, handler) -> None:
        """Register a ledger action. The handler must check action ownership."""
        self.direct_handlers[(channel, kind)] = handler

    def register_preparer(self, fn) -> None:
        """Register async fn(ctx, event, authority) -> InboundEvent | None.

        Prepare verified input before any claim. Return a changed event or None.
        A preparer must preserve the event identity and authority metadata.
        """
        self.preparers.append(fn)

    def register_claim(self, claim) -> None:
        """Register `async claim(ctx, event, authority) -> str | None`.

        A claim runs after the principal is known and before any brain run. It lets an
        area take an event that continues its own flow: a thread reply that answers an
        ask, a corrected note, a pasted sign-in address. Return None to pass. Return a
        text (or "" for no reply) to take the event: the gateway delivers that text and
        no model sees the event. A claim must check that the sender owns the flow.
        """
        self.claims.append(claim)

    def register_payload_filter(self, fn) -> None:
        """Remove sign-in secrets before the gateway stores an event."""
        self.payload_filters.append(fn)

    async def context_authority(self, event, authority, event_id):
        """Thread reads can narrow authority before claims or a brain run."""
        if event.channel == "slack" and event.payload.get("contains_outside_text"):
            authority = replace(authority, profile="tainted", tainted=True)
            async with self.ctx.pool.connection() as conn:
                await conn.execute("UPDATE events SET tainted=true WHERE id=%s", (event_id,))
        return authority

    async def receive(self, event: InboundEvent, adapter: ChannelAdapter | None = None) -> dict:
        """Commit dedupe before a run. A retry cannot start a second run."""
        ctx = self.ctx
        async with ctx.pool.connection() as conn:
            # Keep agent bearer tokens out of the inbox and the audit arguments.
            payload = {"text": "" if event.channel == "board" else event.text,
                       "payload": event.payload, "reply_to": event.reply_to}
            for fn in self.payload_filters:
                payload = fn(payload)
            event_id = await record_event(conn, event, payload)
            if event_id is None:
                return {"decision": "ignore", "duplicate": True}
            authority = await resolve(conn, event, ctx.workspace)
            if not authority:
                await conn.execute("UPDATE events SET status='ignored' WHERE id=%s", (event_id,))
                return {"decision": "ignore", "event_id": event_id}
            if event.channel == "slack" and authority.profile == "team_channel":
                actor = await one(conn, "SELECT key FROM people WHERE slack_id=%s AND active", (event.sender,))
                if actor:
                    await conn.execute("UPDATE events SET payload=payload || %s WHERE id=%s",
                                       (Jsonb({"task_actor": actor["key"]}), event_id))
            await conn.execute(
                "UPDATE events SET principal=%s,person_id=%s,tainted=%s WHERE id=%s",
                (authority.principal, authority.person_id, authority.tainted, event_id),
            )
        adapter = adapter or self.adapters.get(event.channel)
        decision = "direct" if (event.channel, event.kind) in self.direct_handlers else "run"
        try:
            if not adapter:
                raise RuntimeError("The channel has no delivery adapter")
            if len(event.text) > ctx.workspace.config.run_limits.max_message_chars:
                raise ValueError("The message is too long")
            for prepare in self.preparers if event.channel != "board" else ():
                prepared = await prepare(ctx, event, authority)
                if prepared is not None:
                    event = prepared
            authority = await self.context_authority(event, authority, event_id)
            if len(event.text) > ctx.workspace.config.run_limits.max_message_chars:
                raise ValueError("The message is too long")
            if event.channel != "board":
                prepared_payload = {"text": event.text}
                for fn in self.payload_filters:
                    prepared_payload = fn(prepared_payload)
                async with ctx.pool.connection() as conn:
                    await conn.execute(
                        "UPDATE events SET payload=jsonb_set(payload,'{text}',%s) WHERE id=%s",
                        (Jsonb(prepared_payload["text"]), event_id),
                    )
            claimed = None
            claim_event = event
            if event.channel == "slack" and "slack_typed_text" in event.payload:
                claim_event = event.model_copy(update={"text": event.payload["slack_typed_text"]})
            for claim in self.claims if event.channel != "board" else ():
                claimed = await claim(ctx, claim_event, authority)
                if claimed is not None:
                    break
            if claimed is not None:
                decision, text = "claimed", claimed
                if text:
                    await adapter.deliver_final(event, text)
                async with ctx.pool.connection() as conn:
                    await conn.execute(
                        "UPDATE events SET status='handled' WHERE id=%s", (event_id,)
                    )
                return {"decision": decision, "event_id": event_id, "text": text}
            from illo.channels.slack.limits import admit_tainted_run

            if not await admit_tainted_run(ctx, event, authority, event_id):
                return {"decision": "ignore", "event_id": event_id}
            await adapter.show_working(event)
            authority = await self.context_authority(event, authority, event_id)
            if not await admit_tainted_run(ctx, event, authority, event_id):
                await adapter.deliver_final(event, "The channel reached 30 tainted runs in one hour.")
                return {"decision": "ignore", "event_id": event_id}
            if decision == "direct":
                text = await self.direct_handlers[(event.channel, event.kind)](
                    ctx, event, authority
                )
            else:

                async def relay(item):
                    if hasattr(adapter, "show_progress"):
                        await adapter.show_progress(event, item)

                text = await self.run_manager.start_run(event, authority, event_id, relay)
            await adapter.deliver_final(event, text)
            for finalize in self.finalizers:
                try:
                    await finalize(ctx, event, event_id)
                except Exception:
                    # The answer is delivered. A notice that fails has its own retry.
                    log.exception("A finalizer failed after the answer")
            async with ctx.pool.connection() as conn:
                await conn.execute(
                    """
                    UPDATE events SET status='handled',payload=payload || %s WHERE id=%s
                """,
                    (Jsonb({} if event.channel == "board" else {"final_answer": text}), event_id),
                )
            return {"decision": decision, "event_id": event_id, "text": text}
        except ClaimRetry:
            async with ctx.pool.connection() as conn:
                await conn.execute("DELETE FROM events WHERE id=%s AND status='received'", (event_id,))
            return {"decision": "retry", "retryable": True}
        except (Exception, asyncio.CancelledError) as exc:
            category = failure_category(exc)
            async with ctx.pool.connection() as conn:
                await conn.execute("UPDATE events SET status='failed' WHERE id=%s", (event_id,))
            if adapter:
                try:
                    await adapter.deliver_error(event, category=category)
                except Exception:
                    log.error("The channel could not deliver the error")
            if isinstance(exc, asyncio.CancelledError):
                raise
            log.error("The gateway could not finish event %s: category=%s exception_type=%s",
                      event_id, category, type(exc).__name__)
            return {"decision": decision, "event_id": event_id,
                    "error": error_text(category), "category": category}
