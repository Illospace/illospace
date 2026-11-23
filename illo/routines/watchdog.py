"""Notify workspace owners when scheduled jobs stop reporting."""

import asyncio
import logging
from datetime import UTC, datetime

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from illo.db import Connection, all_rows, one
from illo.ledger import kv
from illo.ledger.jobs import report_time, silent_jobs

log = logging.getLogger(__name__)
CHECK_S = 60
CHECK_TIMEOUT_S = 30


async def setup(ctx):
    """Start a separate loop and let the scheduler cancel it on shutdown."""
    ctx.scheduler.tasks.append(asyncio.create_task(_loop(ctx)))


async def _loop(ctx):
    conn = None
    try:
        while True:
            await asyncio.sleep(CHECK_S)
            try:
                async with asyncio.timeout(CHECK_TIMEOUT_S):
                    if conn is None:
                        # A blocked job can hold every connection in the pool.
                        conn = await AsyncConnection.connect(
                            ctx.pool.conninfo, autocommit=True, row_factory=dict_row, connect_timeout=5
                        )
                    await check(ctx, conn)
            except Exception as exc:
                log.error("The job watchdog check failed (%s)", type(exc).__name__)
                if conn is not None:
                    await conn.close()
                    conn = None
    finally:
        if conn is not None:
            await conn.close()


async def _notify(ctx, conn: Connection, text: str, *, alert_key: str | None = None) -> None:
    """Retry an alert if no owner gets the message."""
    sent = False
    try:
        if ctx.slack_client is None:
            return
        owners = await all_rows(
            conn,
            "SELECT key, slack_id FROM people WHERE role='owner' AND active AND slack_id IS NOT NULL ORDER BY key",
        )
        for owner in owners:
            try:
                opened = await ctx.slack_client.conversations_open(users=owner["slack_id"])
                await ctx.slack_client.chat_postMessage(channel=opened["channel"]["id"], text=text)
                sent = True
            except Exception as exc:
                log.warning("The job watchdog could not send a Slack DM (%s)", type(exc).__name__)
    finally:
        if not sent:
            log.warning("The job watchdog did not reach any workspace owner")
            if alert_key is not None:
                await kv.delete(conn, alert_key)


async def check(ctx, conn: Connection, now: datetime | None = None) -> None:
    """Check job sensors and claim notices on an autocommit connection."""
    now = now or datetime.now(UTC)
    names = sorted(ctx.scheduler.names)
    silent = await silent_jobs(conn, now, names=names, started_at=ctx.scheduler.started_at)
    silent_keys = {row["key"] for row in silent}
    for row in silent:
        key = "watchdog:" + row["key"]
        since = row["since"]
        claim = await one(
            conn,
            "INSERT INTO kv (key,value) VALUES (%s,%s) ON CONFLICT (key) DO NOTHING RETURNING key",
            (key, Jsonb({"since": since.isoformat(), "notified_at": now.isoformat()})),
        )
        if claim:
            job = row["job"]
            log.warning("A background job stopped: %s", job)
            await _notify(
                ctx, conn,
                f"A background job stopped: `{job}`. Its last report was at {report_time(since)}. "
                "Work that needs this job can wait too, such as the briefs. "
                "Run `illo doctor` on the Illo host to see the silent jobs.",
                alert_key=key,
            )
    for claim in await all_rows(conn, "SELECT key FROM kv WHERE key LIKE 'watchdog:job:%' ORDER BY key"):
        key = claim["key"]
        job_key = key.removeprefix("watchdog:")
        if job_key in silent_keys:
            continue
        row = await one(conn, "DELETE FROM kv WHERE key=%s RETURNING value", (key,))
        if row and job_key in names:
            job = job_key.removeprefix("job:")
            since = datetime.fromisoformat(row["value"]["since"])
            await _notify(
                ctx, conn,
                f"The background job `{job}` works again. "
                f"It was silent from {report_time(since)} to {report_time(now)}.",
            )
