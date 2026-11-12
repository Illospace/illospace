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


LEASE_SECONDS = 60


async def claim(ctx, conn, event_id, now):
    """Claim an attempt and commit its lease before the outside call."""
    await conn.execute("SELECT pg_advisory_xact_lock(12000001)")
    pending = await one(conn, "SELECT * FROM task_wakes WHERE event_id=%s FOR UPDATE", (event_id,))
    if not pending or pending["finished_at"] or pending["due_at"] > now:
        return None
    row = await one(conn, "SELECT * FROM tasks WHERE id=%s FOR UPDATE", (pending["task_id"],))
    event = await one(conn, "SELECT * FROM task_events WHERE id=%s", (event_id,))
    if not current(row, pending, event):
        await conn.execute("UPDATE task_wakes SET finished_at=%s WHERE event_id=%s", (now, event_id))
        return None
    exhausted = pending["attempts"] >= 3
    pending = await one(conn, """UPDATE task_wakes SET attempts=LEAST(attempts+1,3),due_at=%s
        WHERE event_id=%s RETURNING *""", (now + timedelta(seconds=LEASE_SECONDS), event_id))
    pending["exhausted"] = exhausted
    return pending, row, event


def current(row, pending, event):
    return (row["holder"] == pending["holder"] and row["state"] in {"todo", "working"}
            and (not row["holder_since"] or event["at"] >= row["holder_since"]))


async def deliver(ctx, claimed, now):
    pending, row, event = claimed
    agent = next((a for a in ctx.workspace.agents if "agent:" + a.key == row["holder"]), None)
    registered = wake_types.get(agent.wake.type) if agent else None
    if pending["exhausted"]:
        result = WakeResult(False, "The last wake attempt ended without a receipt.")
    elif not registered:
        result = WakeResult(False, f"The holder is missing or its wake type is unknown: {agent.wake.type if agent else row['holder']}.")
    else:
        try:
            async with ctx.pool.connection() as delivery_conn:
                # Connector writes own their transactions. No graph lock spans this call.
                await delivery_conn.set_autocommit(True)
                try:
                    async with asyncio.timeout(LEASE_SECONDS - 10):
                        result = await registered[1](ctx, delivery_conn, row, agent, pending["reason"], event)
                finally:
                    await delivery_conn.set_autocommit(False)
        except Exception:
            # Exception strings can contain a secret or a complete endpoint address.
            result = WakeResult(False, "The holder could not receive the task notice.")
    async with ctx.pool.connection() as conn:
        await conn.execute("SELECT pg_advisory_xact_lock(12000001)")
        saved = await one(conn, "SELECT * FROM task_wakes WHERE event_id=%s FOR UPDATE", (event["id"],))
        if (saved["finished_at"] or saved["attempts"] != pending["attempts"]
                or saved["due_at"] != pending["due_at"]):
            return
        row = await one(conn, "SELECT * FROM tasks WHERE id=%s FOR UPDATE", (pending["task_id"],))
        if not current(row, pending, event):
            await conn.execute("UPDATE task_wakes SET finished_at=%s WHERE event_id=%s", (now, event["id"]))
            return
        if result.ok:
            await conn.execute("UPDATE tasks SET stopped_at=NULL WHERE id=%s", (row["id"],))
        final = result.ok or pending["attempts"] >= 3
        if final and not result.ok and row["waits_for"] == pending["holder"]:
            target = await tasks.reviewer(conn, ctx.workspace, row)
            await conn.execute("UPDATE tasks SET waits_for=%s WHERE id=%s", (target, row["id"]))
        await tasks._event(conn, row, "code", "woken" if result.ok else "wake_failed", result.text,
                           {"event_id": event["id"], "attempt": min(3, pending["attempts"]), "final": final,
                            "holder": row["holder"], "links": result.links})
        await conn.execute("""UPDATE task_wakes SET due_at=%s,finished_at=%s
            WHERE event_id=%s""", (now + timedelta(seconds=5 * pending["attempts"]), now if final else None, event["id"]))


async def tick(ctx, now=None):
    now = now or datetime.now(UTC)
    async with ctx.pool.connection() as conn:
        pending = await all_rows(conn, "SELECT * FROM task_wakes WHERE finished_at IS NULL AND due_at<=%s ORDER BY event_id LIMIT 50", (now,))
    for entry in pending:
        async with ctx.pool.connection() as conn:
            claimed = await claim(ctx, conn, entry["event_id"], now)
        if claimed:
            await deliver(ctx, claimed, now)


async def setup(ctx):
    async def check_wakes():
        await tick(ctx)

    ctx.every(2, check_wakes)
