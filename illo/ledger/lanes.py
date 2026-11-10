"""Read and update lanes. Stale is computed, never stored."""

from psycopg.types.json import Jsonb

from illo.db import Connection, all_rows, one

DEFAULT_STALE_MINUTES = {
    "t3": 30,
    "claude-code": 30,
    "codex": 30,
    "github": 1440,
    "manual": 1440,
    "email": 1440,
}


async def upsert(
    conn: Connection,
    *,
    source: str,
    external_id: str,
    title: str,
    state: str = "unknown",
    owner_person_id: int | None = None,
    summary: str = "",
    links: list | None = None,
    repo: str | None = None,
    machine: str | None = None,
    visibility: str = "team",
    end_summary: str | None = None,
) -> dict:
    """Refresh the source snapshot and its last signal time."""
    return await one(
        conn,
        """
        INSERT INTO lanes (source,external_id,title,state,owner_person_id,summary,links,repo,machine,visibility,end_summary,closed_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,CASE WHEN %s IN ('done','failed') THEN now() END)
        ON CONFLICT (source,external_id) DO UPDATE SET title=EXCLUDED.title,state=EXCLUDED.state,
          owner_person_id=EXCLUDED.owner_person_id,summary=EXCLUDED.summary,links=EXCLUDED.links,
          repo=EXCLUDED.repo,machine=EXCLUDED.machine,visibility=EXCLUDED.visibility,
          end_summary=EXCLUDED.end_summary,closed_at=EXCLUDED.closed_at,last_signal_at=now(),updated_at=now()
        RETURNING *
    """,
        (
            source,
            external_id,
            title,
            state,
            owner_person_id,
            summary,
            Jsonb(links or []),
            repo,
            machine,
            visibility,
            end_summary,
            state,
        ),
    )


async def add_event(
    conn: Connection, lane_id: int, kind: str, text: str, data: dict | None = None
) -> dict:
    """Append a source event and refresh the lane signal time."""
    await conn.execute(
        "UPDATE lanes SET last_signal_at=now(),updated_at=now() WHERE id=%s", (lane_id,)
    )
    return await one(
        conn,
        """
        INSERT INTO lane_events (lane_id,kind,text,data) VALUES (%s,%s,%s,%s) RETURNING *
    """,
        (lane_id, kind, text, Jsonb(data or {})),
    )


def _select() -> str:
    return """
        SELECT l.*, (state = 'working' AND last_signal_at < now() -
          COALESCE((%s::jsonb ->> source)::integer,30) * interval '1 minute') AS stale
        FROM lanes l
    """


async def get(
    conn: Connection, lane_id: int, *, principal: str = "team", stale_minutes: dict | None = None
) -> dict | None:
    """Only an owner can read a private lane."""
    return await one(
        conn,
        _select()
        + """
        WHERE l.id = %s AND (visibility = 'team' OR owner_person_id =
          (SELECT id FROM people WHERE 'person:' || key = %s AND active))
    """,
        (Jsonb(stale_minutes or DEFAULT_STALE_MINUTES), lane_id, principal),
    )


async def search(
    conn: Connection,
    query: str = "",
    *,
    principal: str = "team",
    owner: str | None = None,
    state: str | None = None,
    limit: int = 20,
    stale_minutes: dict | None = None,
) -> list[dict]:
    """Search visible lanes by text, owner key, and state. Include source events."""
    return await all_rows(
        conn,
        _select()
        + """
        WHERE (visibility = 'team' OR owner_person_id = (SELECT id FROM people WHERE 'person:' || key = %s AND active))
          AND (%s::text IS NULL OR owner_person_id = (SELECT id FROM people WHERE key = %s))
          AND (%s::text IS NULL OR state = %s)
          AND (state <> 'open' OR %s::text <> '' OR %s::text IS NOT NULL)
          AND (concat_ws(' ',title,summary) ILIKE %s OR EXISTS
            (SELECT 1 FROM lane_events e WHERE e.lane_id=l.id AND e.text ILIKE %s))
        ORDER BY CASE WHEN state IN ('working','review','waiting_person','blocked') THEN 0
          WHEN state='open' THEN 1 ELSE 2 END,updated_at DESC,id DESC LIMIT %s
    """,
        (
            Jsonb(stale_minutes or DEFAULT_STALE_MINUTES),
            principal,
            owner,
            owner,
            state,
            state,
            query.strip(),
            state,
            f"%{query}%",
            f"%{query}%",
            max(1, min(limit, 50)),
        ),
    )


async def compact_search(conn, *, principal, owner=None, query="", state=None, repo=None,
                         limit=15, stale_minutes=None, attention=False):
    """Filter visible rows and counts in SQL before any compact text reaches the model."""
    params = (principal, owner, owner, repo, repo, query, f"%{query}%", f"%{query}%")
    visible = """
        FROM lanes l LEFT JOIN people p ON p.id=l.owner_person_id
        WHERE (l.visibility='team' OR l.owner_person_id=(
          SELECT id FROM people WHERE 'person:'||key=%s AND active))
          AND (%s::text IS NULL OR p.key=%s)
          AND (%s::text IS NULL OR l.repo=%s)
          AND (%s='' OR concat_ws(' ',l.title,l.summary) ILIKE %s OR EXISTS
            (SELECT 1 FROM lane_events e WHERE e.lane_id=l.id AND e.text ILIKE %s))
    """
    counts = await all_rows(conn, "SELECT l.state,count(*) AS n,bool_or(l.visibility='private') AS private " + visible + " GROUP BY l.state", params)
    if attention:
        selection = """AND (l.state IN ('waiting_person','blocked','failed') OR
          (l.state='working' AND l.last_signal_at<now()-
          COALESCE((%s::jsonb->>l.source)::integer,30)*interval '1 minute'))"""
        selected_params = (Jsonb(stale_minutes or DEFAULT_STALE_MINUTES),)
    elif state:
        selection, selected_params = "AND l.state=%s", (state,)
    elif query.strip():
        selection, selected_params = "", ()
    else:
        selection = """AND l.state IN ('working','waiting_person','blocked','review','failed')
          AND l.last_signal_at>=now()-interval '7 days'"""
        selected_params = ()
    rows = await all_rows(conn, """
        SELECT l.id,l.state,l.title,p.key AS owner,l.source,l.last_signal_at,l.links,l.visibility,
          left(l.summary,200) AS summary,
          (SELECT left(e.text,200) FROM lane_events e WHERE e.lane_id=l.id
            ORDER BY e.at DESC,e.id DESC LIMIT 1) AS last_event,
          (l.state='working' AND l.last_signal_at<now()-
            COALESCE((%s::jsonb->>l.source)::integer,30)*interval '1 minute') AS stale,
          count(*) OVER () AS total
    """ + visible + selection + " ORDER BY l.last_signal_at DESC,l.id DESC LIMIT %s",
        (Jsonb(stale_minutes or DEFAULT_STALE_MINUTES), *params, *selected_params, max(1,min(limit,40))))
    return rows, {r['state']: r['n'] for r in counts}, any(r['private'] for r in counts)
