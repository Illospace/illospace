"""Send drafts only after the requester approves the stored card in a DM."""

import logging
from datetime import UTC, datetime, timedelta

from illo.asks import cards
from illo.asks.service import post_dm
from illo.ledger import ask_workflow as ledger
from illo.ledger import drafts, runs, sensors
from illo.tools.registry import ToolContext

log = logging.getLogger(__name__)


async def sync_card(ctx, conn, draft):
    if not ctx.slack_client:
        await sensors.error(
            conn, "drafts:slack", "Slack is not connected. Draft cards wait in the ledger."
        )
        return
    from illo.sources.github import WRITE_TOOLS, prepare_draft_card

    if draft["kind"] in WRITE_TOOLS:
        await prepare_draft_card(ctx, conn, draft)
    blocks = cards.draft_card(draft)
    digest = cards.digest(blocks)
    sent = {"channel": draft["slack_channel"], "ts": draft["slack_ts"]}
    if draft["slack_ts"]:
        if draft["card_digest"] != digest:
            await ctx.slack_client.chat_update(
                channel=draft["slack_channel"],
                ts=draft["slack_ts"],
                text=f"Draft for {draft['destination']}",
                blocks=blocks,
            )
    elif draft["state"] == "pending":
        person = await ledger.draft_owner(conn, draft)
        sent = await post_dm(
            ctx, person, f"Draft for {draft['destination']}", blocks, "draft", draft["id"]
        )
    else:
        return
    if sent:
        await ledger.save_draft_card(conn, draft["id"], sent, digest)
        await sensors.ok(conn, "drafts:slack")
    return sent


async def tick(ctx):
    async with ctx.pool.connection() as conn:
        await ledger.expire(conn)
        ids = await ledger.drafts_to_check(conn)
    for row in ids:
        try:
            async with ctx.pool.connection() as conn:
                draft = await ledger.lock_draft(conn, row["id"])
                await sync_card(ctx, conn, draft)
        except Exception:
            log.error("Could not refresh draft %s", row["id"])
            async with ctx.pool.connection() as conn:
                await sensors.error(
                    conn, "drafts:slack", "A draft card could not be sent or updated."
                )


def can_perform(ctx, draft):
    return (
        draft["kind"] in ctx.draft_performers
        or draft["kind"] == "slack_post"
        or draft["kind"] in ctx.registry.tools
    )


async def perform(ctx, draft):
    """Use the shared performer hook, or the exact registered tool handler."""
    fn = ctx.draft_performers.get(draft["kind"])
    if fn:
        await fn(ctx, draft)
        return
    if draft["kind"] == "slack_post":
        if not ctx.slack_client or not draft["destination"].startswith("channel:"):
            raise ValueError("Slack is not connected or the destination is invalid")
        content = draft["content"]
        await ctx.slack_client.chat_postMessage(
            channel=draft["destination"][8:], text=content["text"], blocks=content.get("blocks")
        )
        return
    # A registry draft stores the validated arguments. Approval authorizes this one write.
    tool = ctx.registry.tools[draft["kind"]]
    args = tool.arguments.model_validate(draft["content"]["arguments"]).model_dump()
    if (
        draft["content"].get("tool") != tool.name
        or tool.destination(args) != draft["destination"]
        or tool.risk not in {"write_internal", "send_outside"}
    ):
        raise ValueError("The draft no longer matches the registered action")
    async with ctx.pool.connection() as conn:
        run = await runs.get(conn, draft["run_id"])
        if run["person_id"] != draft["approver_person_id"]:
            raise ValueError("The requester does not own this draft")
        await tool.handler(ToolContext(conn, run, ctx), args)


async def handle(ctx, person, action):
    parts = action.get("action_id", "").split(":")
    if len(parts) != 3 or not parts[1].isdigit() or parts[2] not in {"approve", "reject"}:
        return
    draft_id = int(parts[1])
    approved = False
    async with ctx.pool.connection() as conn:
        draft = await ledger.lock_draft(conn, draft_id)
        if (
            not draft
            or not person["active"]
            or draft["approver_person_id"] != person["id"]
            or not draft["slack_channel"]
            or not draft["slack_channel"].startswith("D")
            or action.get("channel") != draft["slack_channel"]
            or action.get("message_ts") != draft["slack_ts"]
        ):
            return
        run = await runs.get(conn, draft["run_id"])
        if not run or run["person_id"] != person["id"]:
            return
        if draft["state"] != "pending":
            await sync_card(ctx, conn, draft)
            return
        if draft["created_at"] + timedelta(hours=24) <= datetime.now(UTC):
            await ledger.expire_draft(conn, draft_id)
        elif parts[2] == "approve" and cards.draft_preview(draft) is None:
            return
        elif parts[2] == "approve" and not can_perform(ctx, draft):
            if ctx.slack_client:
                await ctx.slack_client.chat_postMessage(
                    channel=draft["slack_channel"],
                    thread_ts=draft["slack_ts"],
                    text="This action is not connected. The draft stays unsent.",
                )
            return
        else:
            approved = parts[2] == "approve"
            decided = await drafts.decide(
                conn, draft_id, person_id=person["id"], approve=approved, in_dm=True
            )
            approved = approved and decided
            draft = await ledger.lock_draft(conn, draft_id)
    # Commit the decision before the external action. A second click or a restart cannot send again.
    if approved:
        try:
            await perform(ctx, draft)
        except Exception:
            async with ctx.pool.connection() as conn:
                await ledger.draft_failed(conn, draft_id)
                await sensors.error(
                    conn,
                    f"draft:{draft_id}",
                    "Draft delivery could not be confirmed. Check the destination.",
                )
        else:
            async with ctx.pool.connection() as conn:
                await drafts.mark_sent(conn, draft_id)
    async with ctx.pool.connection() as conn:
        draft = await ledger.lock_draft(conn, draft_id)
        await sync_card(ctx, conn, draft)
