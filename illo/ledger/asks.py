"""Keep ask answers and their at-least-once delivery state in Postgres."""

from psycopg.types.json import Jsonb

from illo.db import Connection, all_rows, one
from illo.ledger import mcp, rules


async def create(
    conn: Connection,
    *,
    owner_person_id: int | None = None,
    question: str,
    kind: str = "decision",
    options: list[dict] | None = None,
    recommended_option: str | None = None,
    proof: dict | None = None,
    lane_id: int | None = None,
    requester_token_id: int | None = None,
    requester_person_id: int | None = None,
    due_at=None,
    expires_at=None,
    source_ref: str | None = None,
    area: str | None = None,
    allow_rule: bool = True,
    permission: bool = False,
) -> dict:
    """Create an open ask. Option IDs must be unique and the recommendation must exist."""
    options = options or []
    if any(
        not isinstance(o, dict)
        or not isinstance(o.get("id"), str)
        or not isinstance(o.get("label"), str)
        for o in options
    ):
        raise ValueError("Each option needs an id and label")
    ids = [o["id"] for o in options]
    if len(ids) != len(set(ids)) or (
        recommended_option is not None and recommended_option not in ids
    ):
        raise ValueError("Use unique option IDs and a valid recommended option")
    if requester_token_id is not None:
        # Both direct MCP calls and model tools must hold the same token lock.
        await conn.execute("SELECT id FROM tokens WHERE id=%s FOR UPDATE", (requester_token_id,))
        await mcp.check_open_ask_limit(conn, requester_token_id, owner_person_id)
    return await one(
        conn,
        """
        INSERT INTO asks (owner_person_id,question,kind,options,recommended_option,proof,lane_id,
                          requester_token_id,requester_person_id,due_at,expires_at,source_ref,
                          area,allow_rule,permission)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *
    """,
        (
            owner_person_id,
            question,
            kind,
            Jsonb(options),
            recommended_option,
            Jsonb(proof or {}),
            lane_id,
            requester_token_id,
            requester_person_id,
            due_at,
            expires_at,
            source_ref,
            area,
            allow_rule,
            permission,
        ),
    )


async def get(conn: Connection, ask_id: int, *, principal: str = "team") -> dict | None:
    """Hide private lanes. Without a lane, only the ask's participants can read it."""
    return await one(
        conn,
        """
        SELECT a.*,COALESCE(l.visibility,'private') AS visibility,
          p.key AS owner,l.title AS lane_title,l.links AS lane_links
        FROM asks a LEFT JOIN lanes l ON l.id=a.lane_id
        LEFT JOIN people p ON p.id=a.owner_person_id
        LEFT JOIN people me ON 'person:' || me.key=%s AND me.active
        WHERE a.id=%s AND (l.visibility='team' OR l.owner_person_id=me.id
          OR (l.id IS NULL AND me.id IN (a.owner_person_id,a.requester_person_id)))
    """,
        (principal, ask_id),
    )


async def list_open(
    conn: Connection, *, owner_person_id: int | None = None, principal: str = "team"
) -> list[dict]:
    """List open asks by owner with the same lane visibility filter."""
    return await all_rows(
        conn,
        """
        SELECT a.*,COALESCE(l.visibility,'private') AS visibility,
          p.key AS owner,l.title AS lane_title,l.links AS lane_links
        FROM asks a LEFT JOIN lanes l ON l.id=a.lane_id
        LEFT JOIN people p ON p.id=a.owner_person_id
        LEFT JOIN people me ON 'person:' || me.key=%s AND me.active
        WHERE a.state='open'
          AND (%s::bigint IS NULL OR a.owner_person_id=%s)
          AND (l.visibility='team' OR l.owner_person_id=me.id
            OR (l.id IS NULL AND me.id IN (a.owner_person_id,a.requester_person_id)))
        ORDER BY a.due_at NULLS LAST,a.id
    """,
        (principal, owner_person_id, owner_person_id),
    )


async def answer(
    conn: Connection,
    ask_id: int,
    *,
    person_id: int,
    option: str | None = None,
    text: str | None = None,
    via: str = "person",
) -> dict | None:
    """Only the owner can answer an open, unexpired ask."""
    if via not in {"person", "approved_draft"}:
        raise ValueError("Use a person answer or an approved draft")
    if option is None and not text:
        raise ValueError("Choose an option or give an answer")
    return await one(
        conn,
        """
        UPDATE asks SET state='answered',answer_option=%s,answer_text=%s,answered_by_person_id=%s,
          answered_via=%s,answered_at=now(),delivery_state='pending'
        WHERE id=%s AND owner_person_id=%s AND state='open' AND (expires_at IS NULL OR expires_at>now())
          AND (%s::text IS NULL OR EXISTS (SELECT 1 FROM jsonb_array_elements(options) o WHERE o->>'id'=%s))
        RETURNING *
    """,
        (option, text, person_id, via, ask_id, person_id, option, option),
    )


async def answer_with_rule(conn: Connection, ask_id: int, rule_id: int) -> dict | None:
    """Use only a matching owner rule and an option the agent proposed."""
    async with conn.transaction():
        ask = await one(
            conn,
            "SELECT * FROM asks WHERE id=%s AND state='open' AND (expires_at IS NULL OR expires_at>now()) FOR UPDATE",
            (ask_id,),
        )
        rule = await one(
            conn, "SELECT * FROM rules WHERE id=%s AND removed_at IS NULL FOR UPDATE", (rule_id,)
        )
        if not ask or not rule or not ask["allow_rule"]:
            return None
        ask = {**ask, "requester_person_id": await rules.requester_person_id(conn, ask)}
        lane = (
            await one(conn, "SELECT * FROM lanes WHERE id=%s FOR SHARE", (ask["lane_id"],))
            if ask["lane_id"]
            else None
        )
        if (
            ask["source_ref"]
            and lane
            and (lane["state"] in {"done", "failed"} or lane["closed_at"])
        ):
            return None
        option = rules.matching_option(rule, ask["options"])
        if option is None or not rules.matches(rule, ask, lane):
            return None
        owner = await one(
            conn, "SELECT id FROM people WHERE id=%s AND active", (ask["owner_person_id"],)
        )
        if not owner:
            return None
        row = await one(
            conn,
            """
            UPDATE asks SET state='answered',answer_option=%s,answered_via='rule',rule_id=%s,
              answer_text=NULL,
              answered_at=now(),delivery_state='pending' WHERE id=%s RETURNING *
        """,
            (option["id"], rule_id, ask_id),
        )
        await rules.count_use(conn, rule_id)
        return row


async def cancel(conn: Connection, ask_id: int) -> bool:
    """Cancel an open ask after its source closes it. Trusted channel code calls this."""
    return bool(
        await one(
            conn,
            "UPDATE asks SET state='cancelled' WHERE id=%s AND state='open' RETURNING id",
            (ask_id,),
        )
    )


async def expire(conn: Connection) -> int:
    """Expire open asks and stop delivery after the answer's end date."""
    cursor = await conn.execute(
        "UPDATE asks SET state='expired' WHERE state='open' AND expires_at<=now()"
    )
    await conn.execute("""
        UPDATE asks SET delivery_state='failed' WHERE state='answered'
          AND (expires_at<=now() OR answered_at<=now()-interval '30 minutes')
          AND delivery_state IN ('pending','delivered')
    """)
    return cursor.rowcount


async def pull(conn: Connection, token_id: int) -> list[dict]:
    """Give unacked answers again until the requester acknowledges them."""
    await expire(conn)
    return await all_rows(
        conn,
        """
        UPDATE asks SET delivery_state='delivered',delivery_attempts=delivery_attempts+1,delivered_at=now()
        WHERE requester_token_id=%s AND state='answered' AND delivery_state IN ('pending','delivered')
          AND (expires_at IS NULL OR expires_at>now()) RETURNING *
    """,
        (token_id,),
    )


async def ack(conn: Connection, ask_id: int, token_id: int) -> bool:
    """Only the requester can acknowledge an answer."""
    await expire(conn)
    return bool(
        await one(
            conn,
            """
        UPDATE asks SET delivery_state='acked',acked_at=now()
        WHERE id=%s AND requester_token_id=%s AND delivery_state='delivered' RETURNING id
    """,
            (ask_id, token_id),
        )
    )


async def compact_list(conn, *, principal, owner=None, limit=15):
    """Limit ask rows in SQL and preserve the private-read signal."""
    return await all_rows(conn, """
        SELECT a.id,a.kind,left(a.question,160) AS question,a.due_at,p.key AS owner,
          l.title AS lane_title,l.links AS lane_links,
          COALESCE(l.visibility,'private') AS visibility,count(*) OVER () AS total,
          bool_or(COALESCE(l.visibility,'private')='private') OVER () AS _private
        FROM asks a LEFT JOIN lanes l ON l.id=a.lane_id
        LEFT JOIN people p ON p.id=a.owner_person_id
        LEFT JOIN people me ON 'person:'||me.key=%s AND me.active
        WHERE a.state='open' AND (%s::text IS NULL OR p.key=%s)
          AND (l.visibility='team' OR l.owner_person_id=me.id
            OR (l.id IS NULL AND me.id IN (a.owner_person_id,a.requester_person_id)))
        ORDER BY a.due_at NULLS LAST,a.id LIMIT %s
    """, (principal, owner, owner, max(1,min(limit,40))))
