"""Send memory notices after the answer and name the memory that a correction is about."""

from illo.db import all_rows, one
from illo.gateway import InboundEvent
from illo.ledger import sensors
from illo.memory import buttons


async def _dm_channel(ctx, conn, person_id):
    person = await one(conn, "SELECT slack_id FROM people WHERE id=%s AND active", (person_id,))
    if not person or not person.get("slack_id"):
        return None
    try:
        opened = await ctx.slack_client.conversations_open(users=person["slack_id"])
    except Exception:
        await sensors.error(conn, "memory", "The memory is saved. The DM notice could not be opened.")
        return None
    return (opened.get("channel") or {}).get("id")


async def deliver_notices(ctx, event, event_id):
    if event.channel not in {"slack", "mcp"} or not ctx.slack_client:
        return
    slack_channel = event.payload.get("channel_id") if event.channel == "slack" else None
    if event.channel == "slack" and not slack_channel:
        return
    thread = (event.payload.get("thread_ts") or event.reply_to) if event.channel == "slack" else None
    async with ctx.pool.connection() as conn:
        rows = await all_rows(conn, """SELECT m.* FROM memory_changes m JOIN runs r ON r.id=m.run_id
            WHERE r.event_id=%s AND m.slack_ts IS NULL AND m.state='saved'
            ORDER BY m.id FOR UPDATE OF m""", (event_id,))
        dm_cache = {}
        for row in rows:
            channel = slack_channel
            if event.channel == "mcp":
                pid = row["person_id"]
                if pid not in dm_cache:
                    dm_cache[pid] = await _dm_channel(ctx, conn, pid)
                channel = dm_cache[pid]
            if channel:
                await buttons.notice(ctx, conn, row, channel, thread)


async def setup(ctx):
    ctx.gateway.register_finalizer(deliver_notices)
    ctx.gateway.register_preparer(buttons.prepare)
    ctx.slack_actions.register("memory:", buttons.action)

    async def retry_notices():
        async with ctx.pool.connection() as conn:
            events = await all_rows(conn, """SELECT DISTINCT e.id,e.channel,e.external_id,e.kind,e.payload
                FROM memory_changes m JOIN runs r ON r.id=m.run_id JOIN events e ON e.id=r.event_id
                WHERE m.slack_ts IS NULL AND m.state='saved' AND e.channel IN ('slack','mcp')
                AND e.status='handled' AND m.created_at > now() - interval '7 days'""")
        for row in events:
            event = InboundEvent(channel=row["channel"], external_id=row["external_id"], kind=row["kind"],
                payload=row["payload"].get("payload", {}), reply_to=row["payload"].get("reply_to"))
            await deliver_notices(ctx, event, row["id"])

    ctx.every(ctx.config.workspace_check_seconds, retry_notices)
