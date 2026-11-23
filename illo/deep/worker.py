"""Claim one deep run. An expired lease fails and never runs a second time."""

import asyncio
from contextlib import suppress

from psycopg.types.json import Jsonb

from illo.brain.routes import resolve_route
from illo.brain.run_manager import DeepContext
from illo.db import one
from illo.deep.delivery import send
from illo.gateway.events import InboundEvent, record_event
from illo.gateway.principal import Authority
from illo.gateway.replies import failure_category
from illo.ledger import audit, sensors
from illo.ledger.runs import expire_run

WORKER_LOCK = 15001501


async def recover(conn):
    rows = await (
        await conn.execute("""UPDATE deep_runs SET state='failed',ended_at=now(),
        error='The deep run lease expired.' WHERE state='running' AND lease_until<clock_timestamp()
        RETURNING *""")
    ).fetchall()
    for row in rows:
        if row["run_id"]:
            await expire_run(conn, row["run_id"], row["error"])
    return rows


def _route_timeout(ctx):
    # The worker uses the route timeout only for the lease and the result wait.
    return resolve_route(ctx.workspace, "deep").timeout_s


async def claim(ctx, conn, only_id=None):
    if await one(conn, "SELECT id FROM deep_runs WHERE state='running' LIMIT 1"):
        return None
    row = await one(
        conn,
        "SELECT * FROM deep_runs WHERE state='queued' AND (%s::bigint IS NULL OR id=%s) ORDER BY (kind='check'),id FOR UPDATE SKIP LOCKED LIMIT 1",
        (only_id, only_id),
    )
    if not row:
        return None
    return await one(
        conn,
        """UPDATE deep_runs SET state='running',started_at=now(),
        lease_until=clock_timestamp()+%s * interval '1 second' WHERE id=%s RETURNING *""",
        (_route_timeout(ctx), row["id"]),
    )


def _deep_context(row):
    context = row["context"]
    person_id = row["person_id"] if row["principal"].startswith("person:") else None
    return DeepContext(
        deep_run_id=row["id"], kind=row["kind"], deliver=row["deliver"],
        authority=Authority(row["principal"], row["profile"], person_id),
        task_actor=context.get("task_actor"), requester_id=row["person_id"],
        customer_read=bool(context.get("customer_read")), personal_read=bool(context.get("personal_read")),
    )


async def execute(ctx, row):
    state, text, error = "failed", None, None
    event_id = None
    try:
        deep = _deep_context(row)
        context = {**row["context"], "deep_run_id": deep.deep_run_id, "deep_kind": deep.kind,
                   "deliver": deep.deliver}
        event = InboundEvent(channel="deep", external_id=str(deep.deep_run_id), kind=deep.kind,
                             text=row["job"], payload=context)
        payload = {"text": event.text, "payload": context}
        if deep.task_actor:
            payload["task_actor"] = deep.task_actor
        async with ctx.pool.connection() as conn:
            event_id = await record_event(conn, event, payload, authority=deep.authority)
        if event_id is None:
            raise RuntimeError("The deep event already exists")
        text = await ctx.gateway.run_manager.start_run(event, deep.authority, event_id, deep=deep)
        state = "done"
    except asyncio.CancelledError:
        error, state = "The deep work was cancelled.", "cancelled"
        raise
    except Exception as exc:
        error = {
            "timeout": "The run reached its time limit.",
            "provider": "The model service is unavailable.",
            "limit": "The model limit was reached.",
        }.get(failure_category(exc), "The deep run failed.")
    finally:
        async with ctx.pool.connection() as conn:
            current = await one(
                conn,
                "UPDATE deep_runs SET state=%s,result=%s,error=%s,ended_at=now() WHERE id=%s RETURNING *",
                (state, text, error, row["id"]),
            )
            await conn.execute(
                "UPDATE events SET status=%s,payload=payload || %s WHERE id=%s",
                ("handled" if state == "done" else "failed", Jsonb({"final_answer": text}), event_id),
            )
            if current["run_id"]:
                await audit.write(
                    conn,
                    run_id=current["run_id"],
                    principal=row["principal"],
                    tool="deep_run",
                    args={"kind": row["kind"]},
                    decision="allow" if state == "done" else "deny",
                    reason="The deep run is done." if state == "done" else error,
                )
        sandbox = getattr(ctx, "sandbox", None)
        if sandbox is not None and current["run_id"] is not None:
            with suppress(Exception):
                await sandbox.cleanup(current["run_id"])
        if row["kind"] != "brief":
            await deliver_result(ctx, current)
    return current


async def deliver_result(ctx, row):
    text = (
        row["result"]
        if row["state"] == "done"
        else (
            "I could not finish the deep work on this request. Reason: "
            + (
                "The run reached its time limit."
                if row["error"] == "The deep run lease expired."
                else row["error"] or "The deep run failed."
            )
        )
    )
    try:
        await send(ctx, row, text)
    except Exception:
        async with ctx.pool.connection() as conn:
            await sensors.error(
                conn, "deep:delivery", "The deep result is stored. Slack delivery will retry."
            )
    else:
        async with ctx.pool.connection() as conn:
            await conn.execute("UPDATE deep_runs SET delivered_at=now() WHERE id=%s", (row["id"],))


async def tick(ctx, only_id=None):
    # Hold a session lock as well as a row lease. A live worker keeps its slot even
    # if the database clock passes the lease before Python handles cancellation.
    async with ctx.pool.connection() as conn:
        lock = await one(conn, "SELECT pg_try_advisory_lock(%s) AS held", (WORKER_LOCK,))
        await conn.commit()
        if not lock["held"]:
            return None
        try:
            expired = await recover(conn)
            await conn.commit()
            for stale in expired:
                if getattr(ctx, "sandbox", None) is not None and stale["run_id"]:
                    with suppress(Exception):
                        await ctx.sandbox.cleanup(stale["run_id"])
            pending = await (
                await conn.execute(
                    "SELECT * FROM deep_runs WHERE state IN ('done','failed','cancelled') AND kind<>'brief' AND delivered_at IS NULL ORDER BY id LIMIT 10"
                )
            ).fetchall()
            await conn.commit()
            for previous in pending:
                await deliver_result(ctx, previous)
            row = await claim(ctx, conn, only_id)
            await conn.commit()
            return await execute(ctx, row) if row else None
        finally:
            await conn.execute("SELECT pg_advisory_unlock(%s)", (WORKER_LOCK,))
            await conn.commit()


async def result(ctx, deep_id):
    """Brief schedules retain their existing delivery lock and fallback path."""
    async with asyncio.timeout(_route_timeout(ctx)):
        while True:
            await tick(ctx, deep_id)
            async with ctx.pool.connection() as conn:
                row = await one(conn, "SELECT * FROM deep_runs WHERE id=%s", (deep_id,))
            if row["state"] in {"done", "failed", "cancelled"}:
                return row
            await asyncio.sleep(1)
