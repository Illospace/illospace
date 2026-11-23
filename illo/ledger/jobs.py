"""Read scheduled jobs that have stopped reporting."""

from collections.abc import Iterable
from datetime import UTC, datetime

from illo.db import Connection, all_rows

# Allow ten job intervals and at least ten minutes between reports.
SILENT_FACTOR = 10
SILENT_MIN_S = 600


async def silent_jobs(
    conn: Connection, now: datetime, *,
    names: Iterable[str] | None = None, started_at: datetime | None = None,
) -> list[dict]:
    """Use the latest report or the process start to measure silence."""
    sql = """SELECT key,last_ok_at,last_error_at,expected_every_s
             FROM sensors WHERE key LIKE %s AND expected_every_s IS NOT NULL"""
    params: list[str | list[str]] = ["job:%"]
    if names is not None:
        sql += " AND key=ANY(%s)"
        params.append(list(names))
    rows = await all_rows(conn, sql + " ORDER BY key", params)
    silent = []
    for row in rows:
        since = max(
            (at for at in (row["last_ok_at"], row["last_error_at"], started_at) if at is not None),
            default=None,
        )
        limit_s = max(SILENT_MIN_S, SILENT_FACTOR * row["expected_every_s"])
        if since is not None and (now - since).total_seconds() > limit_s:
            key = row["key"]
            silent.append({"key": key, "job": key.removeprefix("job:"), "since": since, "limit_s": limit_s})
    return silent


def report_time(when: datetime) -> str:
    """Show report times in UTC."""
    return when.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
