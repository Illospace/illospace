"""Send at most one due reminder for each ask."""

from datetime import UTC, datetime

from illo.asks.service import is_live, message_id
from illo.ledger import ask_workflow as ledger

REMINDER = "This still waits for you. The agent cannot continue without it."


async def remind(ctx):
    if not ctx.slack_client:
        return
    async with ctx.pool.connection() as conn:
        ids = await ledger.due_asks(conn)
    for row in ids:
        async with ctx.pool.connection() as conn:
            ask = await ledger.lock_ask(conn, row["id"])
            if (
                not await is_live(conn, ask)
                or ask["reminded_at"]
                or (ask["expires_at"] and ask["expires_at"] <= datetime.now(UTC))
            ):
                continue
            await ctx.slack_client.chat_postMessage(
                channel=ask["slack_channel"],
                thread_ts=ask["slack_ts"],
                text=REMINDER,
                client_msg_id=message_id("reminder", ask["id"]),
            )
            await ledger.mark_reminded(conn, ask["id"])


async def setup(ctx):
    async def check_reminders():
        await remind(ctx)

    ctx.every(2, check_reminders)
