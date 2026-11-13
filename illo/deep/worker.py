"""Claim one deep run. An expired lease fails and never runs a second time."""

import asyncio
from contextlib import suppress

from psycopg.types.json import Jsonb

from illo.brain.run_manager import RunOptions
from illo.db import one
from illo.deep.delivery import send
from illo.deep.ports import refresh_repos
from illo.gateway import InboundEvent
from illo.gateway.principal import Authority
from illo.gateway.replies import failure_category
from illo.ledger import audit, runs, sensors

WORKER_LOCK = 15001501


async def recover(conn):
    rows = await (
        await conn.execute("""UPDATE deep_runs SET state='failed',ended_at=now(),
        error='The deep run lease expired.' WHERE state='running' AND lease_until<clock_timestamp()
        RETURNING *""")
    ).fetchall()
    for row in rows:
        if row["run_id"]:
            await runs.finish(conn, row["run_id"], "timeout", error=row["error"])
            await conn.execute(
                "UPDATE events SET status='failed' WHERE id=(SELECT event_id FROM runs WHERE id=%s)",
                (row["run_id"],),
            )
    return rows


async def claim(ctx, conn, only_id=None):
    if await one(conn, "SELECT id FROM deep_runs WHERE state='running' LIMIT 1"):
        return None
    row = await one(
        conn,
        "SELECT * FROM deep_runs WHERE state='queued' AND (%s::bigint IS NULL OR id=%s) ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1",
        (only_id, only_id),
    )
    if not row:
        return None
    return await one(
        conn,
        """UPDATE deep_runs SET state='running',started_at=now(),
        lease_until=clock_timestamp()+%s * interval '1 second' WHERE id=%s RETURNING *""",
        (ctx.workspace.config.brain.deep.timeout_s, row["id"]),
    )


async def execute(ctx, row):
    config = ctx.workspace.config.brain
    settings = config.deep
    state, text, error = "failed", None, None
    inbound = None
    try:
        async with asyncio.timeout(settings.timeout_s):
            context = {
                **row["context"],
                "deep_run_id": row["id"],
                "deep_kind": row["kind"],
                "deliver": row["deliver"],
            }
            event = InboundEvent(
                channel="deep",
                external_id=str(row["id"]),
                kind=row["kind"],
                text=row["job"],
                payload=context,
            )
            authority = Authority(
                row["principal"],
                row["profile"],
                row["person_id"] if row["principal"].startswith("person:") else None,
            )
            async with ctx.pool.connection() as conn:
                inbound = await one(
                    conn,
                    """INSERT INTO events(channel,external_id,kind,principal,person_id,payload)
                    VALUES ('deep',%s,%s,%s,%s,%s) RETURNING id""",
                    (
                        event.external_id,
                        event.kind,
                        authority.principal,
                        authority.person_id,
                        Jsonb(
                            {
                                "text": event.text,
                                "payload": context,
                                **(
                                    {"task_actor": context["task_actor"]}
                                    if context.get("task_actor")
                                    else {}
                                ),
                            }
                        ),
                    ),
                )
            if row["person_id"]:
                async with ctx.pool.connection() as conn:
                    if not await one(
                        conn, "SELECT id FROM people WHERE id=%s AND active", (row["person_id"],)
                    ):
                        raise ValueError("The person is inactive")
            await refresh_repos(ctx)
            # Personal runs keep their existing initial provider-search limit.
            options = RunOptions(
                row["id"],
                row["kind"],
                settings.model or ctx.config.brain_model or config.model,
                settings.effort,
                settings.max_turns,
                settings.timeout_s,
                settings.max_output_tokens,
                ctx.workspace.config.web.search == "provider"
                and row["kind"] != "brief"
                and row["profile"] != "person_dm",
                customer_read=bool(row["context"].get("customer_read")),
                personal_read=bool(row["context"].get("personal_read")),
            )
            text = await ctx.gateway.run_manager.execute(
                event, authority, inbound["id"], overrides=options
            )
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
            stored = await one(conn, "SELECT run_id FROM deep_runs WHERE id=%s", (row["id"],))
            if stored["run_id"] is None and inbound:
                # A failed sign-in or setup still has a terminal ledger run.
                failed = await runs.create(
                    conn,
                    principal=row["principal"],
                    person_id=authority.person_id,
                    profile=row["profile"],
                    runtime=ctx.config.brain_runtime or config.runtime,
                    model=settings.model or ctx.config.brain_model or config.model,
                    event_id=inbound["id"],
                )
                await conn.execute(
                    "UPDATE runs SET deep_run_id=%s,deep_kind=%s,tool_limit=%s WHERE id=%s",
                    (row["id"], row["kind"], settings.max_turns, failed["id"]),
                )
                await runs.finish(conn, failed["id"], "failed", error=error)
                await conn.execute(
                    "UPDATE deep_runs SET run_id=%s WHERE id=%s", (failed["id"], row["id"])
                )
            current = await one(
                conn,
                "UPDATE deep_runs SET state=%s,result=%s,error=%s,ended_at=now() WHERE id=%s RETURNING *",
                (state, text, error, row["id"]),
            )
            await conn.execute(
                "UPDATE events SET status=%s,payload=payload || %s WHERE channel='deep' AND external_id=%s",
                (
                    "handled" if state == "done" else "failed",
                    Jsonb({"final_answer": text}),
                    str(row["id"]),
                ),
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
    async with asyncio.timeout(ctx.workspace.config.brain.deep.timeout_s):
        while True:
            await tick(ctx, deep_id)
            async with ctx.pool.connection() as conn:
                row = await one(conn, "SELECT * FROM deep_runs WHERE id=%s", (deep_id,))
            if row["state"] in {"done", "failed", "cancelled"}:
                return row
            await asyncio.sleep(1)
