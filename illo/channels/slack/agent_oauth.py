"""Catch pairing messages before the gateway. No code reaches a model or memory."""

import asyncio
import re
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

from illo.asks.cards import button
from illo.channels.slack.pairing import pairing_code, sent_by_app
from illo.db import one
from illo.ledger import agent_oauth as ledger
from illo.slackapi import dm_person

FAILURE = "The code is invalid, used, or expired. Start again in the app."


def card(row, by_app=False):
    host = urlsplit(row["redirect_uri"]).hostname
    text = (f"App name (not verified): {row['name']}\nReturn address: {host}\n"
            "It can read and write Illo memory, tasks and lanes as you.\n"
            # An app can send a code that the person did not ask for. The card says who sent it.
            + ("An app sent this code in your name.\n" if by_app else "")
            + "Approve only if you started this yourself a moment ago.")
    if host in {"localhost", "127.0.0.1", "::1"}:
        text += "\nThis address returns to a local app. Check that you started this sign-in."
    return text, [
        {"type": "section", "text": {"type": "plain_text", "text": text}},
        {"type": "actions", "elements": [
            button("Approve", f"agent-oauth:{row['id']}:approve", True),
            button("Deny", f"agent-oauth:{row['id']}:deny"),
        ]},
    ]


async def receive(ctx, raw, *, outside_workspace=False):
    code = pairing_code(raw.get("text", ""))
    if not code:
        return False
    if (outside_workspace or raw.get("bot_id") or raw.get("bot_profile") or raw.get("workflow_id")
            or raw.get("subtype") == "bot_message"):
        return True
    from illo.tasks.pages.signin import receive as browser_receive

    if await browser_receive(ctx, raw, code):
        return True
    async with ctx.pool.connection() as conn:
        row = await ledger.pair(conn, code, raw.get("user", ""), raw["channel"], raw["ts"])
    if row and row.get("duplicate"):
        return True
    if not row:
        await ctx.slack_client.chat_postMessage(channel=raw["channel"], text=FAILURE)
        return True
    text, blocks = card(row, sent_by_app(raw.get("text", "")))
    sent = await ctx.slack_client.chat_postMessage(channel=raw["channel"], text=text, blocks=blocks,
                                                 unfurl_links=False, unfurl_media=False,
                                                 client_msg_id=str(uuid5(NAMESPACE_URL, f"illo:agent-oauth:{row['id']}")))
    async with ctx.pool.connection() as conn:
        await conn.execute("UPDATE agent_oauth_requests SET slack_ts=%s WHERE id=%s", (sent["ts"], row["id"]))
    return True


async def action(ctx, person, action):
    match = re.fullmatch(r"agent-oauth:([1-9][0-9]*):(approve|deny)", action.get("action_id", ""))
    channel = action.get("channel", "")
    if not match or not channel or not channel.startswith("D"):
        return
    request_id, choice = match.groups()
    async with ctx.pool.connection() as conn:
        result = await ledger.decide(conn, int(request_id), person["id"], channel,
                                     action.get("message_ts"), choice == "approve")
    if result:
        await notify(ctx, int(request_id))
        await ctx.slack_client.chat_update(channel=channel, ts=action["message_ts"], text=result,
                                          blocks=[{"type": "section", "text": {"type": "plain_text", "text": result}}])


async def notify(ctx, request_id):
    # Reserve one attempt and commit before Slack. A failed DM cannot undo consent.
    try:
        async with ctx.pool.connection() as conn:
            row = await one(conn, """UPDATE agent_oauth_requests r SET notice_at=clock_timestamp()
                FROM tokens t, people p WHERE r.id=%s AND r.decision='approved'
                AND r.notice_at IS NULL AND t.id=r.token_id AND p.id=t.person_id
                RETURNING t.label,p.key""", (request_id,))
        if row:
            async with asyncio.timeout(1):
                await dm_person(ctx, row["key"], f"You connected {row['label']} to Illo. "
                                "If this was not you, tell your operator at once.")
    except Exception:
        pass
