"""Keep proposed writes separate from approved writes."""

from psycopg.types.json import Jsonb

from illo.db import Connection, all_rows, one


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


async def decide_outside_ticket(
    conn: Connection, draft_id: int, *, person_id: int, approve: bool
) -> bool:
    """Only the approver can decide a code-created outside-ticket assignment draft from its thread."""
    return bool(
        await one(
            conn,
            """
        UPDATE drafts SET state=%s,decided_at=now()
        WHERE id=%s AND approver_person_id=%s AND state='pending'
          AND kind='github_assign_issue' AND content ? 'outside_ticket'
        RETURNING id
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


async def pending_for_tickets(
    conn: Connection, ticket_urls: list[str], *, viewer_person: str | None = None
) -> list[dict]:
    """Return pending assignment drafts linked to any of the given ticket URLs."""
    if not ticket_urls:
        return []
    rows = await all_rows(
        conn,
        """SELECT d.id, d.kind, d.content, d.created_at, p.name AS approver_name
        FROM drafts d
        JOIN people p ON p.id = d.approver_person_id
        WHERE d.state = 'pending'
          AND d.kind = 'github_assign_issue'
          AND d.content->'github_issue'->>'link' = ANY(%s)
          AND (d.content ? 'outside_ticket' OR p.key = %s)
        ORDER BY d.id DESC""",
        (ticket_urls, viewer_person),
    )
    return [
        {
            "draft_id": d["id"],
            "kind": d["kind"],
            "repo": (d["content"].get("arguments") or {}).get("repo"),
            "number": (d["content"].get("arguments") or {}).get("number"),
            "assignee": (d["content"].get("arguments") or {}).get("assignee"),
            "approver": d["approver_name"],
            "created_at": d["created_at"],
        }
        for d in rows
    ]
