"""Use a unique key as an atomic claim across workers."""

from illo.db import Connection, one


async def take(conn: Connection, key: str, run_id: int, note: str = "") -> bool:
    """Only the first caller receives true."""
    return bool(
        await one(
            conn,
            """
        INSERT INTO claims (key,run_id,note) VALUES (%s,%s,%s) ON CONFLICT (key) DO NOTHING RETURNING id
    """,
            (key, run_id, note),
        )
    )
