"""Verify schema creation, restart safety, and per-file rollback."""

import pytest
from psycopg.errors import CheckViolation, ForeignKeyViolation, UndefinedTable

from illo.db import all_rows, migrate, one


async def test_migrations_apply_twice(pool):
    async with pool.connection() as conn:
        assert await migrate(conn) == []
        assert await migrate(conn) == []
        records = await all_rows(conn, "SELECT name FROM schema_migrations")
        assert records == [{"name": "0001_init.sql"}]
        names = {
            r["tablename"]
            for r in await all_rows(
                conn, "SELECT tablename FROM pg_tables WHERE schemaname='public'"
            )
        }
        assert {
            "people",
            "tokens",
            "events",
            "runs",
            "audit",
            "lanes",
            "lane_events",
            "asks",
            "rules",
            "items",
            "submissions",
            "drafts",
            "signals",
            "claims",
            "connector_grants",
            "secrets",
            "routine_state",
            "sensors",
            "kv",
        } <= names
        indexes = await all_rows(conn, "SELECT indexdef FROM pg_indexes WHERE tablename='items'")
        assert any("USING gin (tsv)" in r["indexdef"] for r in indexes)


async def test_each_migration_rolls_back_as_one_file(pool, tmp_path):
    (tmp_path / "9999_fail.sql").write_text(
        "CREATE TABLE should_rollback (id int); SELECT * FROM missing_table;"
    )
    async with pool.connection() as conn:
        with pytest.raises(UndefinedTable):
            await migrate(conn, tmp_path)
        assert (await one(conn, "SELECT to_regclass('should_rollback') AS name"))["name"] is None
        assert not await one(conn, "SELECT name FROM schema_migrations WHERE name='9999_fail.sql'")


async def test_schema_checks_states_and_foreign_keys(conn):
    with pytest.raises(CheckViolation):
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO lanes (source,external_id,title,state) VALUES ('github','1','x','stale')"
            )

    with pytest.raises(ForeignKeyViolation):
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO tokens (person_id,label,token_hash) VALUES (999,'test','hash')"
            )
