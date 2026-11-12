"""Send memory notices after the answer and claim corrections before a model run."""

from illo.db import all_rows
from illo.gateway import InboundEvent
from illo.memory import buttons


async def deliver_notices(ctx, event, event_id):
    if event.channel != "slack" or not ctx.slack_client:
        return
    channel = event.payload.get("channel_id")
    if not channel:
        return
    thread = event.payload.get("thread_ts") or event.reply_to
    async with ctx.pool.connection() as conn:
        rows = await all_rows(conn, """SELECT m.* FROM memory_changes m JOIN runs r ON r.id=m.run_id
            WHERE r.event_id=%s AND m.slack_ts IS NULL AND m.state='saved'
            ORDER BY m.id FOR UPDATE OF m""", (event_id,))
        for row in rows:
            await buttons.notice(ctx, conn, row, channel, thread)


async def setup(ctx):
    ctx.gateway.register_finalizer(deliver_notices)
    ctx.gateway.register_claim(buttons.claim)
    ctx.slack_actions.register("memory:", buttons.action)

    async def retry_notices():
        async with ctx.pool.connection() as conn:
            events = await all_rows(conn, """SELECT DISTINCT e.id,e.external_id,e.kind,e.payload
                FROM memory_changes m JOIN runs r ON r.id=m.run_id JOIN events e ON e.id=r.event_id
                WHERE m.slack_ts IS NULL AND m.state='saved' AND e.channel='slack'
                AND e.status='handled' AND m.created_at > now() - interval '7 days'""")
        for row in events:
            event = InboundEvent(channel="slack", external_id=row["external_id"], kind=row["kind"],
                payload=row["payload"].get("payload", {}), reply_to=row["payload"].get("reply_to"))
            await deliver_notices(ctx, event, row["id"])

    ctx.every(ctx.config.workspace_check_seconds, retry_notices)
