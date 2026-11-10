"""Accept each source report once."""

from psycopg.types.json import Jsonb

from illo.db import Connection, one


async def insert(
    conn: Connection,
    *,
    source: str,
    dedupe_key: str,
    severity: str,
    title: str,
    body: str,
    data: dict | None = None,
) -> dict | None:
    """Return None on a duplicate without changing the first report."""
    return await one(
        conn,
        """
        INSERT INTO signals (source,dedupe_key,severity,title,body,data) VALUES (%s,%s,%s,%s,%s,%s)
        ON CONFLICT (source,dedupe_key) DO NOTHING RETURNING *
    """,
        (source, dedupe_key, severity, title, body, Jsonb(data or {})),
    )
