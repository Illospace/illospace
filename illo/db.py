"""Small async Postgres helpers. Callers own their transactions."""

from pathlib import Path
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

Connection = AsyncConnection[dict[str, Any]]


def create_pool(url: str, *, min_size: int = 1, max_size: int = 10) -> AsyncConnectionPool:
    """Return a closed pool. Open it inside the app lifespan."""
    return AsyncConnectionPool(
        url,
        min_size=min_size,
        max_size=max_size,
        open=False,
        kwargs={"row_factory": dict_row},
    )


async def one(conn: Connection, sql: str, params=None) -> dict | None:
    """Read one row as a mapping."""
    return await (await conn.execute(sql, params)).fetchone()


async def all_rows(conn: Connection, sql: str, params=None) -> list[dict]:
    """Read all rows as mappings."""
    return await (await conn.execute(sql, params)).fetchall()


async def migrate(conn: Connection, directory: Path | None = None) -> list[str]:
    """Apply each new SQL file in one transaction. Serialize concurrent runners."""
    directory = directory or Path(__file__).with_name("migrations")
    applied = []
    # A session lock also protects the initial schema_migrations creation.
    await conn.execute("SELECT pg_advisory_lock(697637104)")
    await conn.commit()
    try:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now()
            )
        """)
        await conn.commit()
        for path in sorted(directory.glob("*.sql")):
            async with conn.transaction():
                exists = await one(
                    conn, "SELECT name FROM schema_migrations WHERE name = %s", (path.name,)
                )
                if exists:
                    continue
                await conn.execute(path.read_text())
                await conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
                applied.append(path.name)
    finally:
        await conn.execute("SELECT pg_advisory_unlock(697637104)")
        await conn.commit()
    return applied
