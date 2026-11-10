"""Check the index upgrade on invented rows without a table rewrite."""

from pathlib import Path
from time import perf_counter

from illo.db import all_rows, migrate, one

MIGRATIONS = Path(__file__).parents[2] / "illo" / "migrations"


async def test_existing_rows_get_parts_index_and_runner_skips_repeat(pool, tmp_path):
    # Use a separate schema so the test exercises an upgrade, not an empty table.
    async with pool.connection() as conn:
        try:
            await conn.execute("CREATE SCHEMA ranking_upgrade")
            await conn.execute("SET search_path TO ranking_upgrade,public")
            await conn.execute("CREATE TABLE schema_migrations (name text PRIMARY KEY)")
            for table in ("items", "lanes", "events"):
                await conn.execute(f"CREATE TABLE {table} (LIKE public.{table} "
                                   "INCLUDING DEFAULTS INCLUDING GENERATED INCLUDING IDENTITY)")
            await conn.execute("""INSERT INTO items (kind,source_ref,title,body)
                SELECT 'archive','archive:sample-'||n,'Acme photo cost review',
                  repeat('Compare photo costs and credits. ',8)||'mira.orbit@example.com'
                FROM generate_series(1,6000) n""")
            await conn.execute("""INSERT INTO lanes (source,external_id,title,state)
                SELECT 'manual','sample-'||n,'Acme photo costs','done' FROM generate_series(1,1300) n""")
            await conn.execute("""INSERT INTO events (channel,external_id,kind,principal,payload)
                SELECT 'slack','C_ACME_ALERTS:'||n,'message','team',
                  '{"text":"mira.orbit@example.com asks about credits"}'::jsonb
                FROM generate_series(1,6000) n""")
            await conn.commit()
            before = await one(conn, "SELECT relfilenode FROM pg_class WHERE oid='items'::regclass")
            for name in ("0402_search_ranking.sql", "0403_search_event_parts.sql"):
                (tmp_path / name).write_text((MIGRATIONS / name).read_text())
                started = perf_counter()
                assert await migrate(conn, tmp_path) == [name]
                print(f"{name}: {perf_counter()-started:.3f}s; 6000 items, 1300 lanes, 6000 events")
            assert await migrate(conn, tmp_path) == []
            after = await one(conn, "SELECT relfilenode FROM pg_class WHERE oid='items'::regclass")
            assert after == before
            assert (await one(conn, """SELECT count(*) AS n FROM items WHERE
                illo_search_vector(title,summary,body) @@ illo_search_query('simple','orbit')"""))["n"] == 6000
            await conn.execute("SET LOCAL enable_seqscan=off")
            for table, vector in [("items", "illo_search_vector(title,summary,body)"),
                                  ("events", "illo_search_vector('','',coalesce(payload->>'text',''))")]:
                guard = " AND channel='slack' AND principal='team'" if table == "events" else ""
                plan = await all_rows(conn, f"EXPLAIN SELECT * FROM {table} WHERE "
                                      f"{vector} @@ illo_search_query('simple','orbit'){guard}")
                assert "Bitmap Index Scan" in str(plan)
        finally:
            await conn.rollback()
            # A pool connection must not retain the test schema.
            await conn.execute("RESET search_path")
            await conn.execute("DROP SCHEMA IF EXISTS ranking_upgrade CASCADE")
            await conn.commit()
