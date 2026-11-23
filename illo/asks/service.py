"""Route asks and refresh their cards from the ledger every two seconds."""

import logging
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from illo.asks import cards, proof
from illo.connectors.http import private_http_logs
from illo.db import one
from illo.ledger import ask_workflow as ledger
from illo.ledger import asks, board, rules, sensors
from illo.tasks import pages

log = logging.getLogger(__name__)


def message_id(kind, row_id):
    return str(uuid5(NAMESPACE_URL, f"illo:{kind}:{row_id}"))


async def post_dm(ctx, person, text, blocks, kind, row_id, *, metadata=None, channel=None):
    """Use a stable message ID across a retry after an uncertain Slack response."""
    if not ctx.slack_client or not person or not person["slack_id"]:
        return None
    if channel is None:
        opened = await ctx.slack_client.conversations_open(users=person["slack_id"])
        channel = opened["channel"]["id"]
    fields = {"blocks": blocks} if blocks else {}
    if metadata:
        if not channel.startswith("D"):
            return None
        fields.update(metadata=metadata, unfurl_links=False, unfurl_media=False)
    sent = await ctx.slack_client.chat_postMessage(
        channel=channel, text=text, client_msg_id=message_id(kind, row_id), **fields
    )
    return {"channel": channel, "ts": sent["ts"]}


async def sync_card(ctx, conn, ask, *, force=False):
    if await asks.is_board_ask(conn, ask):
        from illo.board import acl

        thread = await acl.thread_access(conn, await board.ask_thread_id(conn, ask["id"]))
        owner = await one(conn, "SELECT * FROM people WHERE id=%s", (ask["owner_person_id"],))
        if not thread or not await acl.recipient_ok(conn, thread, owner):
            return
        if board.is_coordinator_ask(ask):
            escalation = await one(conn, "SELECT recipient_slack_user FROM board_escalations WHERE ask_id=%s",
                                   (ask["id"],))
            if not ask["slack_ts"] or not escalation or escalation["recipient_slack_user"] != owner["slack_id"]:
                return
    if not ctx.slack_client:
        await sensors.error(
            conn, "asks:slack", "Slack is not connected. Ask cards wait in the ledger."
        )
        return
    data = await ledger.card_data(conn, ask)
    if (
        data["lane"]
        and data["lane"]["visibility"] == "private"
        and data["lane"]["owner_person_id"] != ask["owner_person_id"]
    ):
        await sensors.error(
            conn,
            "asks:owner",
            "A private lane and its ask have different owners. The card stays unsent.",
        )
        return
    if not data["owner"]:
        await sensors.error(conn, "asks:owner", "An ask owner is missing or inactive.")
        return
    await prepare_card(ctx, conn, data)
    blocks = cards.ask_card(data)
    digest = cards.digest(blocks)
    sent = {"channel": ask["slack_channel"], "ts": ask["slack_ts"]}
    if ask["slack_ts"]:
        if force or ask["card_digest"] != digest:
            with private_http_logs():
                await ctx.slack_client.chat_update(
                    channel=ask["slack_channel"],
                    ts=ask["slack_ts"],
                    text=cards.fallback(data),
                    blocks=blocks,
                )
    elif ask["state"] == "open":
        sent = await post_dm(ctx, data["owner"], cards.fallback(data), blocks, "ask", ask["id"])
    else:
        return
    if sent:
        await ledger.save_ask_card(conn, ask["id"], sent, digest)
        await sensors.ok(conn, "asks:slack")


async def refresh_ask(ctx, ask_id, *, force=False):
    async with ctx.pool.connection() as conn:
        ask = await ledger.lock_ask(conn, ask_id)
        if ask:
            await sync_card(ctx, conn, ask, force=force)


async def tick(ctx, *, force=False):
    async with ctx.pool.connection() as conn:
        await ledger.close_stale(conn)
        await ledger.expire(conn)
        ids = await ledger.asks_to_check(conn)
    for row in ids:
        try:
            async with ctx.pool.connection() as conn:
                ask = await ledger.lock_ask(conn, row["id"])
                if ask["state"] == "open":
                    ask = await ledger.route(conn, ask, ctx.workspace.config.default_owner)
                    rule = await rules.match_ask(conn, ask) if await is_live(conn, ask) else None
                    if rule:
                        ask = await asks.answer_with_rule(conn, ask["id"], rule["id"]) or ask
                ask = await ledger.lock_ask(conn, row["id"])
                await sync_card(ctx, conn, ask, force=force)
        except Exception:
            log.error("Could not refresh ask %s", row["id"])
            async with ctx.pool.connection() as conn:
                await sensors.error(conn, "asks:slack", "An ask card could not be sent or updated.")
    async with ctx.pool.connection() as conn:
        await ledger.record_decisions(conn)


async def open_asks_summary(person, ctx=None):
    """Accept a loaded snapshot, or load one for a prompt caller with a context."""
    rows = person.get("asks", [])
    if ctx is not None:
        async with ctx.pool.connection() as conn:
            rows = await asks.list_open(
                conn, owner_person_id=person["id"], principal=f"person:{person['key']}"
            )
    if not rows:
        return "You have no open asks."
    return f"You have {len(rows)} open asks:\n" + "\n".join(
        f"- {row['question']} (ask {row['id']})" for row in rows
    )


async def is_live(conn, ask):
    """Close source asks before a click can answer a finished lane."""
    if ask["state"] != "open":
        return False
    if ask["expires_at"] and ask["expires_at"] <= datetime.now(UTC):
        await ledger.expire_ask(conn, ask["id"])
        return False
    if ask["source_ref"] and ask["lane_id"]:
        # Lock the source state while the answer commits.
        lane = await ledger.lock_lane_state(conn, ask["lane_id"])
        if lane and (lane["state"] in {"done", "failed"} or lane["closed_at"]):
            await asks.cancel(conn, ask["id"])
            return False
    return True


async def prepare_card(ctx, conn, data):
    data['page_url'] = None
    if pages.enabled(ctx):
        destination = (await pages.person_path(conn, ctx.workspace, data['owner'], data['id'], ask=True)
                       if data.get('owner') else f"/web/a/{data['id']}")
        data['page_url'] = pages.base_url(ctx) + destination
    if board.is_coordinator_ask(data):
        from illo.board.policy import board_pages_ready

        thread_id = await board.ask_thread_id(conn, data["id"])
        data["page_url"] = pages.base_url(ctx) + f"/web/board/t/{thread_id}" if board_pages_ready(ctx) else None
        data["page_action"] = f"web-open-board:{thread_id}"
    requester = data.get('requester') or {}
    data['full_proof'] = not pages.enabled(ctx) and requester.get('scopes') == ['agent']
    if data['full_proof'] and data.get('owner'):
        data['saved_items'] = await proof.saved_items(conn, data['proof'], 'person:' + data['owner']['key'])
    return data


async def answer(conn, ask_id, person_id, *, option=None, text=None, workspace=None):
    """All direct person answers lock the ask and write the same decision."""
    from illo.board.asks import can_answer, on_answer

    ask = await board.lock_for_ask(conn, ask_id)
    owner = await one(conn, 'SELECT id FROM people WHERE id=%s AND active FOR SHARE', (person_id,))
    if not ask or not owner or ask['owner_person_id'] != person_id or not await is_live(conn, ask):
        return None
    if not await can_answer(conn, ask, person_id):
        return None
    if option is None and not (text and text.strip()):
        return None
    async with conn.transaction():
        answered = await asks.answer(conn, ask_id, person_id=person_id, option=option, text=text)
        if answered:
            await board.record_decision(conn, answered, workspace=workspace)
            await ledger.decision(conn, answered)
            await on_answer(conn, answered, workspace=workspace)
        return answered
