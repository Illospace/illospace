"""Manage run records and short-lived bearer tokens."""

import secrets

from psycopg.types.json import Jsonb

from illo.db import Connection, one
from illo.ledger.tokens import digest


async def create(
    conn: Connection,
    *,
    principal: str,
    person_id: int | None,
    profile: str,
    runtime: str,
    model: str,
    event_id: int | None = None,
    tainted: bool = False,
) -> dict:
    """The gateway sets the principal once, before the brain starts."""
    return await one(
        conn,
        """
        INSERT INTO runs (principal,person_id,profile,runtime,model,event_id,tainted)
        VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *
    """,
        (principal, person_id, profile, runtime, model, event_id, tainted),
    )


async def get(conn: Connection, run_id: int) -> dict | None:
    """Read a run without exposing its bearer hash."""
    row = await one(conn, "SELECT * FROM runs WHERE id = %s", (run_id,))
    if row:
        row.pop("token_hash", None)
    return row


async def mint_token(conn: Connection, run_id: int, timeout_s: float) -> str:
    """Replace the run token and bound its life to the run timeout."""
    token = secrets.token_urlsafe(32)
    row = await one(
        conn,
        """
        UPDATE runs SET token_hash = %s, token_expires_at = now() + %s * interval '1 second'
        WHERE id = %s AND status = 'running' RETURNING id
    """,
        (digest(token), timeout_s, run_id),
    )
    if not row:
        raise ValueError("The run is not active")
    return token


async def verify_token(conn: Connection, token: str, *, lock: bool = False) -> dict | None:
    """Read authority from the stored run, never from tool arguments."""
    return await one(
        conn,
        """
        SELECT r.*, (SELECT e.channel FROM events e WHERE e.id=r.event_id) AS event_channel
        FROM runs r
        WHERE token_hash = %s AND status = 'running' AND token_expires_at > clock_timestamp()
          AND (person_id IS NULL OR EXISTS (SELECT 1 FROM people p WHERE p.id = r.person_id AND p.active))
    """
        + (" FOR UPDATE OF r" if lock else ""),
        (digest(token),),
    )


async def expire_token(conn: Connection, run_id: int) -> None:
    """End bearer access even if the caller still has the random token."""
    await conn.execute(
        "UPDATE runs SET token_hash = NULL, token_expires_at = now() WHERE id = %s", (run_id,)
    )


async def finish(
    conn: Connection,
    run_id: int,
    status: str = "completed",
    *,
    usage: dict | None = None,
    error: str | None = None,
) -> None:
    """Finish once and revoke access in the same statement."""
    if status not in {"completed", "failed", "timeout"}:
        raise ValueError("Use a terminal run status")
    await conn.execute(
        """
        UPDATE runs SET status = %s, usage = usage || %s, error = %s, ended_at = now(),
          token_hash = NULL, token_expires_at = now()
        WHERE id = %s AND status = 'running'
    """,
        (status, Jsonb(usage or {}), error, run_id),
    )
