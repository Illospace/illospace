"""Bound automation runs and owner notices with durable, serialized counters."""

from datetime import UTC, datetime

from illo.db import all_rows, one
from illo.ledger import kv, people, sensors

NOTICE_PREFIX = "slack:owner-notices:"


def now():
    return datetime.now(UTC)


async def send_owner(ctx, conn, owner, text):
    """Reuse the caller's connection, including when the pool has one connection."""
    person = await people.get(conn, owner)
    if not ctx.slack_client or not person or not person.get("slack_id"):
        raise ValueError("Slack is not connected to the owner")
    opened = await ctx.slack_client.conversations_open(users=person["slack_id"])
    return await ctx.slack_client.chat_postMessage(channel=opened["channel"]["id"], text=text)


async def lock(conn, key):
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (key,))


async def admit_tainted_run(ctx, event, authority, event_id):
    """Count admissions from events, including concurrent events and thread taint."""
    if event.channel != "slack" or not authority.tainted:
        return True
    channel = event.payload["channel_id"]
    key = f"slack:automation:{channel}"
    async with ctx.pool.connection() as conn:
        await lock(conn, key)
        current = await one(conn, "SELECT payload FROM events WHERE id=%s", (event_id,))
        if current["payload"].get("automation_admitted"):
            return True
        count = await one(conn, """
            SELECT count(*) AS n FROM events
            WHERE channel='slack' AND tainted AND created_at > now()-interval '1 hour'
              AND payload->'payload'->>'channel_id'=%s
              AND payload->>'automation_admitted'='true'
        """, (channel,))
        if count["n"] >= 30:
            await conn.execute("UPDATE events SET status='ignored' WHERE id=%s", (event_id,))
            reported = await one(conn, """
                SELECT 1 FROM sensors WHERE key=%s AND last_error_at>now()-interval '1 hour'
            """, (key,))
            if not reported:
                await sensors.error(conn, key, "The channel reached 30 tainted runs in one hour")
            return False
        await conn.execute("""
            UPDATE events SET payload=payload || '{"automation_admitted":true}'::jsonb
            WHERE id=%s
        """, (event_id,))
    return True


def fresh_state(state, timestamp):
    state = state or {"sent": [], "pending": [], "due_at": None}
    state["sent"] = [at for at in state["sent"] if at > timestamp - 3600]
    return state


async def notify_unassigned(ctx, conn, run, url):
    """Send one immediate notice per run, with at most six notices per hour."""
    owner = ctx.workspace.config.default_owner
    key = NOTICE_PREFIX + owner
    await lock(conn, key)
    timestamp = now().timestamp()
    state = fresh_state(await kv.get(conn, key), timestamp)
    run_key = f"slack:owner-notified-run:{run['id']}"
    if await kv.get(conn, run_key) or len(state["sent"]) >= 6:
        if url not in state["pending"]:
            state["pending"].append(url)
        if state["due_at"] is None:
            state["due_at"] = min(state["sent"], default=timestamp) + 3600
    else:
        # Record the attempt before Slack. An uncertain delivery must not repeat.
        await kv.set(conn, run_key, True)
        state["sent"].append(timestamp)
        await kv.set(conn, key, state)
        source = " from outside text" if run["tainted"] else ""
        try:
            sent = await send_owner(ctx, conn, owner, f"New ticket{source}, not assigned: {url}. "
                                   "Assign it in GitHub, or tell me in a DM.")
            if not sent:
                raise ValueError("Slack is not connected")
        except Exception:
            await sensors.error(conn, "github:owner-notice", "The owner notice could not be sent")
            state["pending"].append(url)
            state["due_at"] = state["due_at"] or timestamp + 3600
    await kv.set(conn, key, state)


async def flush_owner_notices(ctx):
    """Send one digest when the notice window ends. Keep its links after a failure."""
    async with ctx.pool.connection() as conn:
        owners = await all_rows(conn, "SELECT key FROM kv WHERE starts_with(key,%s)", (NOTICE_PREFIX,))
    for row in owners:
        async with ctx.pool.connection() as conn:
            key = row["key"]
            await lock(conn, key)
            timestamp = now().timestamp()
            state = fresh_state(await kv.get(conn, key), timestamp)
            if not state["pending"] or state["due_at"] > timestamp or len(state["sent"]) >= 6:
                continue
            state["sent"].append(timestamp)
            try:
                sent = await send_owner(ctx, conn, key[len(NOTICE_PREFIX):],
                                       "Tickets not assigned:\n" + "\n".join(state["pending"]))
                if not sent:
                    raise ValueError("Slack is not connected")
            except Exception:
                await sensors.error(conn, "github:owner-notice", "The owner digest could not be sent")
                state["due_at"] = timestamp + 3600
                await kv.set(conn, key, state)
                continue
            state["pending"] = []
            state["due_at"] = None
            await kv.set(conn, key, state)
