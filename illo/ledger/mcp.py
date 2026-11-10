"""Store agent receipts and check ownership at each delivery boundary."""

from psycopg.types.json import Jsonb

from illo.db import all_rows, one


async def check_open_ask_limit(conn, token_id: int, owner_id: int) -> None:
    """The MCP endpoint holds the token row lock through this check and insert."""
    row = await one(
        conn,
        """SELECT count(*) AS n FROM asks WHERE requester_token_id=%s
        AND owner_person_id=%s AND state='open'""",
        (token_id, owner_id),
    )
    if row["n"] >= 20:
        raise ValueError("The token has reached its limit of 20 open asks for this owner")


async def check_hourly_limit(conn, token_id: int, *, submission: bool = False) -> None:
    """Count the new row too. A refusal rolls it back with the tool transaction."""
    query = (
        """SELECT count(*) AS n FROM submissions WHERE token_id=%s
        AND desired_outcome <> 'preserve_knowledge' AND created_at > now()-interval '1 hour'"""
        if submission
        else """SELECT count(*) AS n FROM signals WHERE token_id=%s
        AND created_at > now()-interval '1 hour'"""
    )
    row = await one(conn, query, (token_id,))
    if row["n"] > 60:
        kind = "requests" if submission else "signals"
        raise ValueError(f"The token has reached its limit of 60 {kind} per hour")


async def submission(conn, event_id: str, person_id: int) -> dict | None:
    return await one(
        conn,
        """SELECT s.* FROM submissions s JOIN tokens t ON t.id=s.token_id
        WHERE s.event_id=%s AND t.person_id=%s""",
        (event_id, person_id),
    )


async def reserve(conn, token_id, event_id, key, envelope, digest) -> tuple[dict, bool]:
    """The unique key serializes retries before any item is written."""
    row = await one(
        conn,
        """INSERT INTO submissions
        (token_id,event_id,idempotency_key,desired_outcome,envelope,envelope_digest,status)
        VALUES (%s,%s,%s,%s,%s,%s,'accepted')
        ON CONFLICT (token_id,idempotency_key) DO NOTHING RETURNING *""",
        (token_id, event_id, key, envelope["desired_outcome"], Jsonb(envelope), digest),
    )
    if row:
        return row, False
    return await one(
        conn,
        "SELECT * FROM submissions WHERE token_id=%s AND idempotency_key=%s",
        (token_id, key),
    ), True


async def finish_submission(conn, event_id, result, status, run_id=None) -> None:
    await conn.execute(
        """UPDATE submissions SET result=%s,status=%s,run_id=%s,
        completed_at=CASE WHEN %s IN ('completed','failed') THEN now() END WHERE event_id=%s""",
        (Jsonb(result), status, run_id, status, event_id),
    )


async def save_item(conn, *, title, body, person_id, visibility, meta) -> dict:
    """Allocate the item and its public handle in the receipt transaction."""
    return await one(
        conn,
        """WITH allocated AS (SELECT nextval(pg_get_serial_sequence('items','id')) AS id)
        INSERT INTO items (id,source_ref,kind,title,body,summary,owner_person_id,
                           visibility,meta,source_at) OVERRIDING SYSTEM VALUE
        SELECT id,'saved:' || id,'saved',%s,%s,%s,%s,%s,%s,now() FROM allocated
        RETURNING id,source_ref,visibility,source_at""",
        (title, body, body[:500], person_id, visibility, Jsonb(meta)),
    )


async def submission_run(conn, event_id) -> dict | None:
    return await one(
        conn,
        """SELECT r.id,r.status,r.ended_at,r.error FROM runs r JOIN events e ON e.id=r.event_id
        WHERE e.channel='mcp' AND e.external_id=%s ORDER BY r.id DESC LIMIT 1""",
        (event_id,),
    )


async def requester_token_for_run(conn, run_id: int) -> int:
    """Resolve the agent token from the authenticated event, never from model text."""
    row = await one(
        conn,
        """SELECT s.token_id FROM runs r
        JOIN events e ON e.id=r.event_id AND e.channel='mcp'
        JOIN submissions s ON s.event_id=e.external_id
        JOIN tokens t ON t.id=s.token_id AND t.person_id=r.person_id
        WHERE r.id=%s AND r.profile='agent_request'""",
        (run_id,),
    )
    if not row:
        raise ValueError("The agent run has no authenticated submission")
    return row["token_id"]


async def recover_submissions(conn) -> None:
    """A process restart must not leave an accepted receipt pending forever.

    Client bearer tokens are never stored. An interrupted request cannot be sent
    again safely. Report the failure without claiming that no tool changed data.
    """
    await conn.execute("""
        UPDATE runs SET status='failed',error='The server stopped before delivery.',
        ended_at=now(),token_hash=NULL,token_expires_at=now()
        WHERE status='running' AND event_id IN (
          SELECT e.id FROM events e JOIN submissions s ON s.event_id=e.external_id
          WHERE e.channel='mcp' AND s.status='accepted')
    """)
    await conn.execute("""
        UPDATE events SET status='failed' WHERE channel='mcp' AND status='received'
        AND external_id IN (SELECT event_id FROM submissions WHERE status='accepted')
    """)
    await conn.execute("""
        UPDATE submissions SET status='failed',completed_at=now(),
        result=result || jsonb_build_object(
          'status','failed','handling_status','failed',
          'failure',jsonb_build_object('category','interrupted',
            'message','The server stopped before it could store the answer.'))
        WHERE status='accepted'
    """)


async def signal_by_key(conn, source, dedupe_key) -> dict | None:
    return await one(
        conn, "SELECT id FROM signals WHERE source=%s AND dedupe_key=%s", (source, dedupe_key)
    )


async def pending_signals(conn) -> list[dict]:
    return await all_rows(
        conn,
        """
        SELECT s.* FROM signals s WHERE s.status='received' AND NOT EXISTS
        (SELECT 1 FROM events e WHERE e.channel='signals' AND e.external_id=s.id::text)
        ORDER BY s.id LIMIT 100
    """,
    )


async def item_ref(conn, item_id, *, principal) -> str | None:
    row = await one(
        conn,
        """SELECT source_ref FROM items WHERE id=%s AND (visibility='team' OR
        owner_person_id=(SELECT id FROM people WHERE 'person:'||key=%s AND active))""",
        (item_id, principal),
    )
    return row["source_ref"] if row else None


async def sync_lane_summary_visibility(conn, lane) -> None:
    await conn.execute(
        """UPDATE items SET visibility=%s,owner_person_id=%s WHERE source_ref=%s""",
        (lane["visibility"], lane["owner_person_id"], f"lane:{lane['id']}:end"),
    )


async def stale_lanes(conn, *, principal, query, owner, limit, stale_minutes) -> list[dict]:
    """Filter before the limit, so fresh lanes cannot hide an older stale lane."""
    return await all_rows(
        conn,
        """SELECT l.*,true AS stale FROM lanes l WHERE state='working'
        AND last_signal_at < now() - COALESCE((%s::jsonb->>source)::int,30)*interval '1 minute'
        AND (visibility='team' OR owner_person_id=
          (SELECT id FROM people WHERE 'person:'||key=%s AND active))
        AND (%s::text IS NULL OR owner_person_id=(SELECT id FROM people WHERE key=%s))
        AND (concat_ws(' ',title,summary) ILIKE %s OR EXISTS
          (SELECT 1 FROM lane_events e WHERE e.lane_id=l.id AND e.text ILIKE %s))
        ORDER BY updated_at DESC,id DESC LIMIT %s""",
        (Jsonb(stale_minutes), principal, owner, owner, f"%{query}%", f"%{query}%", limit),
    )


async def existing_ask(conn, token_id, source_ref) -> dict | None:
    if not source_ref:
        return None
    return await one(
        conn,
        "SELECT * FROM asks WHERE requester_token_id=%s AND source_ref=%s",
        (token_id, source_ref),
    )


async def lane_by_ref(conn, source, external_id, *, lock=False) -> dict | None:
    return await one(
        conn,
        "SELECT * FROM lanes WHERE source=%s AND external_id=%s" + (" FOR UPDATE" if lock else ""),
        (source, external_id),
    )


async def lock_lane_ref(conn, source, external_id) -> None:
    """Serialize the first report too, before the lane row exists."""
    await conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (f"mcp-lane:{source}:{external_id}",),
    )


async def pull_answers(conn, token, scope) -> list[dict]:
    """Another person never gets answers, even with the same machine label."""
    return await all_rows(
        conn,
        """WITH picked AS (
          SELECT a.id FROM asks a JOIN tokens t ON t.id=a.requester_token_id
          LEFT JOIN lanes l ON l.id=a.lane_id
          WHERE t.person_id=%s AND a.state='answered'
          AND a.delivery_state IN ('pending','delivered')
          AND (a.expires_at IS NULL OR a.expires_at>now())
          AND (%s='person' OR a.requester_token_id=%s OR l.machine=%s)
          ORDER BY a.id FOR UPDATE OF a
        ), delivered AS (
          UPDATE asks SET delivery_state='delivered',delivery_attempts=delivery_attempts+1,
          delivered_at=now() WHERE id IN (SELECT id FROM picked) RETURNING *
        ) SELECT a.*, l.source AS lane_source,l.external_id AS lane_external_id,
          p.key AS answered_by, r.text AS rule_text
          FROM delivered a LEFT JOIN lanes l ON l.id=a.lane_id
          LEFT JOIN people p ON p.id=a.answered_by_person_id
          LEFT JOIN rules r ON r.id=a.rule_id ORDER BY a.id""",
        (token["person_id"], scope, token["id"], token["label"]),
    )


async def ack_answer(conn, token, ask_id) -> dict | None:
    return await one(
        conn,
        """UPDATE asks a SET delivery_state='acked',acked_at=COALESCE(acked_at,now())
        FROM tokens t WHERE a.requester_token_id=t.id AND t.person_id=%s AND a.id=%s
        AND a.delivery_state IN ('delivered','acked') RETURNING a.id,a.delivery_state""",
        (token["person_id"], ask_id),
    )


async def cancel_ask(conn, token, ask_id, source_ref) -> dict | None:
    """Source refs belong to the calling token; IDs also permit owner token rotation."""
    row = await one(
        conn,
        """SELECT a.* FROM asks a JOIN tokens t ON t.id=a.requester_token_id
        WHERE t.person_id=%s AND ((%s::bigint IS NOT NULL AND a.id=%s)
          OR (%s::text IS NOT NULL AND a.source_ref=%s AND a.requester_token_id=%s))
        FOR UPDATE OF a""",
        (token["person_id"], ask_id, ask_id, source_ref, source_ref, token["id"]),
    )
    if row and row["state"] == "open":
        await conn.execute("UPDATE asks SET state='cancelled' WHERE id=%s", (row["id"],))
        row["state"] = "cancelled"
    return row
