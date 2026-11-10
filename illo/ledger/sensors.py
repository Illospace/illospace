"""Record the last good and bad observation for each sensor."""

from illo.db import Connection


async def ok(conn: Connection, key: str, detail: str = "") -> None:
    """Record a successful observation."""
    await conn.execute(
        """
        INSERT INTO sensors (key,last_ok_at,detail) VALUES (%s,clock_timestamp(),%s)
        ON CONFLICT (key) DO UPDATE SET last_ok_at=clock_timestamp(),detail=EXCLUDED.detail
    """,
        (key, detail),
    )


async def error(conn: Connection, key: str, detail: str) -> None:
    """Keep the last good time when a sensor fails."""
    await conn.execute(
        """
        INSERT INTO sensors (key,last_error_at,detail) VALUES (%s,clock_timestamp(),%s)
        ON CONFLICT (key) DO UPDATE SET last_error_at=clock_timestamp(),detail=EXCLUDED.detail
    """,
        (key, detail),
    )
