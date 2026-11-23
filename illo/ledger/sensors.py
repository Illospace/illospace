"""Record the last good and bad observation for each sensor."""

from illo.db import Connection, all_rows


async def recent_collectors(conn: Connection) -> list[dict]:
    """Read collectors that reported in the last day."""
    return await all_rows(conn, """
        SELECT key, detail, last_ok_at FROM sensors
        WHERE key LIKE 'collector:%' AND last_ok_at > now() - interval '1 day'
        ORDER BY key
    """)


async def ok(conn: Connection, key: str, detail: str = "", *, expected_every_s: float | None = None) -> None:
    """Record a successful observation."""
    await conn.execute(
        """
        INSERT INTO sensors (key,last_ok_at,detail,expected_every_s) VALUES (%s,clock_timestamp(),%s,%s)
        ON CONFLICT (key) DO UPDATE SET last_ok_at=clock_timestamp(),detail=EXCLUDED.detail,
          expected_every_s=COALESCE(EXCLUDED.expected_every_s,sensors.expected_every_s)
    """,
        (key, detail, expected_every_s),
    )


async def error(conn: Connection, key: str, detail: str, *, expected_every_s: float | None = None) -> None:
    """Keep the last good time when a sensor fails."""
    await conn.execute(
        """
        INSERT INTO sensors (key,last_error_at,detail,expected_every_s) VALUES (%s,clock_timestamp(),%s,%s)
        ON CONFLICT (key) DO UPDATE SET last_error_at=clock_timestamp(),detail=EXCLUDED.detail,
          expected_every_s=COALESCE(EXCLUDED.expected_every_s,sensors.expected_every_s)
    """,
        (key, detail, expected_every_s),
    )
