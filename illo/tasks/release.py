"""Restart silent holders and remind the people who must act."""

from collections import Counter
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from illo.db import all_rows, one
from illo.ledger import kv, tasks


async def _agent(ctx, conn, row, entry, now):
    if row["state"] not in {"todo", "working"} or row["waits_for"] != row["holder"]:
        return
    last = await one(conn, """SELECT max(at) AS at FROM task_events
        WHERE task_id=%s AND kind='woken' AND at>=%s""", (row["id"], row["holder_since"]))
    base = max(t for t in (row["last_activity_at"], row["alive_at"], last["at"]) if t)
    hours = max(0, (now-base).total_seconds()/3600)
    grace = timedelta(minutes=ctx.workspace.config.tasks.stop_grace_minutes)
    stopped = bool(row["stopped_at"]) and now - row["stopped_at"] >= grace
    if not stopped and hours <= entry.silence.agent_hours:
        return
    if await one(conn, "SELECT event_id FROM task_wakes WHERE task_id=%s AND finished_at IS NULL", (row["id"],)):
        return
    count = await one(conn, """SELECT count(*) AS n FROM task_events WHERE task_id=%s
        AND kind='released' AND at>=%s AND NOT coalesce((data->>'requested')::boolean,false)""",
        (row["id"], row["holder_since"]))
    if count["n"] >= ctx.workspace.config.tasks.release_max:
        target = await tasks.reviewer(conn, ctx.workspace, row)
        await conn.execute("UPDATE tasks SET waits_for=%s,stopped_at=NULL WHERE id=%s", (target, row["id"]))
        await tasks._event(conn, row, "code", "state",
            f"The holder is silent after {count['n']} restarts. A person must decide.",
            {"escalated": True, "hours": round(hours, 1), "restarts": count["n"]})
    else:
        await conn.execute("UPDATE tasks SET stopped_at=NULL WHERE id=%s", (row["id"],))
        await tasks._event(conn, row, "code", "released", f"The holder is silent for {hours:.1f} hours.",
                           {"hours": round(hours, 1), "stopped": stopped})


def in_window(workspace, person, now=None):
    start, end = workspace.config.tasks.reminder_window
    hour = (now or datetime.now(UTC)).astimezone(ZoneInfo(person["timezone"])).hour
    return start <= hour < end


async def _person(ctx, conn, row, entry, now, counts, extra):
    target = row["waits_for"]
    person = await one(conn, "SELECT * FROM people WHERE 'person:'||key=%s AND active", (target,))
    if not person:
        return
    if not in_window(ctx.workspace, person, now):
        return
    # Find the start of the current next-move period. Later messages do not reset it.
    events = await all_rows(conn, "SELECT * FROM task_events WHERE task_id=%s ORDER BY id DESC", (row["id"],))
    since = row["created_at"]
    needed = "review the returned work" if row["state"] == "review" else "you hold it"
    for event in events:
        if event["data"].get("waits_for") != target:
            break
        since = event["at"]
        if event["kind"] == "question":
            needed = event["text"]
    period = timedelta(hours=entry.silence.person_hours)
    if now - since <= period:
        return
    if any(e["kind"] == "reminded" and e["data"].get("to") == target and now-e["at"] < period for e in events):
        return
    if counts[target] >= 5:
        extra[target] += 1
        return
    counts[target] += 1
    hours = max(0, (now-since).total_seconds()/3600)
    await tasks._event(conn, row, "code", "reminded",
                       f"This task waits for you since {hours:.0f} hours ago: {needed}", {"to": target})


async def tick(ctx, now=None):
    now = now or datetime.now(UTC)
    async with ctx.pool.connection() as conn:
        lock = await one(conn, "SELECT pg_try_advisory_xact_lock(12000003) AS ok")
        if not lock["ok"]:
            return
        await conn.execute("SELECT pg_advisory_xact_lock(12000001)")
        rows = await all_rows(conn, "SELECT * FROM tasks WHERE state NOT IN ('done','cancelled') ORDER BY id FOR UPDATE")
        counts, extra = Counter(), Counter()
        for row in rows:
            entry = next((e for e in ctx.workspace.lists if e.key == row["list_key"]), None)
            if not entry:
                continue
            if (row["holder"] or "").startswith("agent:"):
                await _agent(ctx, conn, row, entry, now)
            if (row["waits_for"] or "").startswith("person:"):
                await _person(ctx, conn, row, entry, now, counts, extra)
        for target, count in extra.items():
            # One summary joins the last reminder. Slack retries it with that event.
            event = await one(conn, """SELECT * FROM task_events WHERE kind='reminded' AND data->>'to'=%s
                ORDER BY id DESC LIMIT 1""", (target,))
            if event:
                await conn.execute("UPDATE task_events SET text=text || %s WHERE id=%s",
                    (f"\n{count} more tasks wait for you. Ask me: what waits for me?", event["id"]))
        await kv.set(conn, "tasks:last-check", now.isoformat())


async def setup(ctx):
    async def check_tasks():
        # Read the current interval after a workspace reload.
        async with ctx.pool.connection() as conn:
            last = await kv.get(conn, "tasks:last-check")
        if not last or (datetime.now(UTC)-datetime.fromisoformat(last)).total_seconds() >= ctx.workspace.config.tasks.check_minutes * 60:
            await tick(ctx)

    ctx.every(60, check_tasks)
