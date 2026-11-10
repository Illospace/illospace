"""Store small server values as JSON."""

from psycopg.types.json import Jsonb

from illo.db import Connection, one


async def set(conn: Connection, key: str, value) -> None:
    """Insert or replace one value."""
    await conn.execute(
        """
        INSERT INTO kv (key,value) VALUES (%s,%s) ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value
    """,
        (key, Jsonb(value)),
    )


async def get(conn: Connection, key: str, default=None):
    """Read a value or the supplied default."""
    row = await one(conn, "SELECT value FROM kv WHERE key=%s", (key,))
    return row["value"] if row else default


async def delete(conn: Connection, key: str) -> bool:
    """Delete one stored value."""
    return bool(await one(conn, "DELETE FROM kv WHERE key=%s RETURNING key", (key,)))
