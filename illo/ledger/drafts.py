"""Keep proposed writes separate from approved writes."""

from psycopg.types.json import Jsonb

from illo.db import Connection, one


async def create(
    conn: Connection,
    *,
    run_id: int,
    approver_person_id: int,
    kind: str,
    destination: str,
    content: dict,
) -> dict:
    """Record the proposed action without executing its handler."""
    return await one(
        conn,
        """
        INSERT INTO drafts (run_id,approver_person_id,kind,destination,content)
        VALUES (%s,%s,%s,%s,%s) RETURNING *
    """,
        (run_id, approver_person_id, kind, destination, Jsonb(content)),
    )


async def decide(
    conn: Connection, draft_id: int, *, person_id: int, approve: bool, in_dm: bool
) -> bool:
    """Only the requester can decide a pending draft, in a DM."""
    if not in_dm:
        return False
    return bool(
        await one(
            conn,
            """
        UPDATE drafts SET state=%s,decided_at=now() WHERE id=%s AND approver_person_id=%s AND state='pending' RETURNING id
    """,
            ("approved" if approve else "rejected", draft_id, person_id),
        )
    )


async def mark_sent(conn: Connection, draft_id: int) -> bool:
    """Only an approved draft can become sent."""
    return bool(
        await one(
            conn,
            "UPDATE drafts SET state='sent' WHERE id=%s AND state='approved' RETURNING id",
            (draft_id,),
        )
    )
