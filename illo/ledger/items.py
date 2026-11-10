"""Search saved text with a mandatory principal visibility filter."""

from psycopg.types.json import Jsonb

from illo.db import Connection, all_rows, one


async def upsert(
    conn: Connection,
    *,
    source_ref: str,
    title: str,
    kind: str = "saved",
    body: str = "",
    summary: str = "",
    owner_person_id: int | None = None,
    visibility: str = "team",
    links: list | None = None,
    meta: dict | None = None,
    source_at=None,
) -> dict:
    """Replace one source snapshot without duplicating its source reference."""
    return await one(
        conn,
        """
        INSERT INTO items (source_ref,title,kind,body,summary,owner_person_id,visibility,links,meta,source_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (source_ref) DO UPDATE SET title=EXCLUDED.title,kind=EXCLUDED.kind,
          body=EXCLUDED.body,summary=EXCLUDED.summary,owner_person_id=EXCLUDED.owner_person_id,
          visibility=EXCLUDED.visibility,links=EXCLUDED.links,meta=EXCLUDED.meta,source_at=EXCLUDED.source_at
        RETURNING id,source_ref,title,kind,summary,visibility
    """,
        (
            source_ref,
            title,
            kind,
            body,
            summary,
            owner_person_id,
            visibility,
            Jsonb(links or []),
            Jsonb(meta or {}),
            source_at,
        ),
    )


async def get(conn: Connection, source_ref: str, *, principal: str = "team") -> dict | None:
    """Read team text or the principal's own private text."""
    return await one(
        conn,
        """
        SELECT id,created_at,kind,source_ref,title,body,summary,owner_person_id,visibility,links,meta,source_at
        FROM items WHERE source_ref=%s AND (visibility='team' OR owner_person_id=
          (SELECT id FROM people WHERE 'person:' || key=%s AND active))
    """,
        (source_ref, principal),
    )


async def search(
    conn: Connection,
    query: str,
    *,
    principal: str = "team",
    kinds: list[str] | None = None,
    limit: int = 10,
) -> list[dict]:
    """Search both token configurations. Keep private text out of other runs."""
    return await all_rows(
        conn,
        """
        WITH q AS (SELECT websearch_to_tsquery('simple',%s) || websearch_to_tsquery('english',%s) AS query)
        SELECT i.id,i.kind,i.source_ref,i.title,i.summary,i.links,i.source_at,ts_rank(i.tsv,q.query) AS score
        FROM items i,q WHERE i.tsv @@ q.query
          AND (visibility='team' OR owner_person_id=(SELECT id FROM people WHERE 'person:' || key=%s AND active))
          AND (%s::text[] IS NULL OR kind=ANY(%s)) ORDER BY score DESC,i.id DESC LIMIT %s
    """,
        (query, query, principal, kinds, kinds, max(1, min(limit, 50))),
    )
