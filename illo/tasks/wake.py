"""Deliver committed wake events. A failed delivery keeps its retry record."""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx

from illo.connectors.http import private_http_logs
from illo.connectors.store import vault
from illo.db import all_rows, one
from illo.ledger import tasks
from illo.tasks.connectors import WakeResult, wake_types


async def send_webhook(ctx, conn, task, agent, reason, event):
    config = agent.wake
    try:
        token = await vault(ctx).get(conn, config.secret)
    except ValueError:
        token = None
    if not token:
        return WakeResult(False, "The wake secret is missing or cannot be read.")
    payload = {"event": reason, "task": task["id"], "event_id": str(event["id"]),
               "text": f"Read task {task['id']}."}
    for _ in range(2):
        try:
            with private_http_logs():
                async with asyncio.timeout(10):
                    async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False,
                                                 transport=getattr(ctx, "wake_transport", None)) as client:
                        async with client.stream("POST", str(config.url), json=payload,
                                headers={"Authorization": "Bearer " + token}) as response:
                            status = response.status_code
            if 200 <= status < 300:
                return WakeResult(True, "The holder received the task notice.")
            text = f"The wake endpoint returned status {status}."
        except (httpx.HTTPError, TimeoutError):
            text = "The wake endpoint did not answer."
    return WakeResult(False, text)


async def deliver(ctx, conn, pending, now):
    # Keep the graph lock before the outbox row lock in every path.
    await conn.execute("SELECT pg_advisory_xact_lock(12000001)")
    pending = await one(conn, "SELECT * FROM task_wakes WHERE event_id=%s FOR UPDATE", (pending["event_id"],))
    if pending["finished_at"] or pending["due_at"] > now:
        return
    row = await one(conn, "SELECT * FROM tasks WHERE id=%s FOR UPDATE", (pending["task_id"],))
    if row["holder"] != pending["holder"] or row["state"] not in {"todo", "working"}:
        await conn.execute("UPDATE task_wakes SET finished_at=%s WHERE event_id=%s", (now, pending["event_id"]))
        return
    event = await one(conn, "SELECT * FROM task_events WHERE id=%s", (pending["event_id"],))
    if row["holder_since"] and event["at"] < row["holder_since"]:
        await conn.execute("UPDATE task_wakes SET finished_at=%s WHERE event_id=%s", (now, event["id"]))
        return
    agent = next((a for a in ctx.workspace.agents if "agent:" + a.key == row["holder"]), None)
    registered = wake_types.get(agent.wake.type) if agent else None
    if not registered:
        result = WakeResult(False, f"The holder is missing or its wake type is unknown: {agent.wake.type if agent else row['holder']}.")
    else:
        try:
            result = await registered[1](ctx, conn, row, agent, pending["reason"], event)
        except Exception:
            # Exception strings can contain a secret or a complete endpoint address.
            result = WakeResult(False, "The holder could not receive the task notice.")
    if result.ok:
        await conn.execute("UPDATE tasks SET stopped_at=NULL WHERE id=%s", (row["id"],))
    attempt = pending["attempts"] + 1
    final = result.ok or attempt >= 3
    if final and not result.ok:
        target = await tasks.reviewer(conn, ctx.workspace, row)
        await conn.execute("UPDATE tasks SET waits_for=%s WHERE id=%s", (target, row["id"]))
    await tasks._event(conn, row, "code", "woken" if result.ok else "wake_failed", result.text,
                       {"event_id": event["id"], "attempt": attempt, "final": final,
                        "holder": row["holder"], "links": result.links})
    await conn.execute("""UPDATE task_wakes SET attempts=%s,due_at=%s,finished_at=%s
        WHERE event_id=%s""", (attempt, now + timedelta(seconds=5 * attempt), now if final else None, event["id"]))


async def tick(ctx, now=None):
    now = now or datetime.now(UTC)
    async with ctx.pool.connection() as conn:
        pending = await all_rows(conn, "SELECT * FROM task_wakes WHERE finished_at IS NULL AND due_at<=%s ORDER BY event_id LIMIT 50", (now,))
    for entry in pending:
        async with ctx.pool.connection() as conn:
            await deliver(ctx, conn, entry, now)


async def setup(ctx):
    async def check_wakes():
        await tick(ctx)

    ctx.every(2, check_wakes)
