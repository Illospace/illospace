"""Compare indexed recall with the former SQL on a fixed, invented corpus."""

import random
import re
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from statistics import median
from time import perf_counter
from types import SimpleNamespace

import pytest

from illo.db import Connection, all_rows, migrate, one
from illo.sources import search, slack_index
from illo.sources.search import Hit, first_link

NOW = datetime(2026, 5, 20, 12, tzinfo=UTC)
CHANNELS = ["C_ACME", "C_WIDGET"]
QUERIES = [
    "quartz", "release", "disk failure", "widget cache", '"release plan"',
    '"disk failure"', "disk OR garden", "release -failure", "acme-backend",
    "service.module", "worker-node", "mira.orbit@example.com", "mira", "orbit",
    "alpha-beta", "garden", "pricing", "deploy rollback", "cache timeout",
    "widget", "archive decision", '"widget cache"', "missingword", "disk -repair",
    "nebula release", "disk OR cache", "service.module -garden",
    "common", "common quartz", "5", "5.5", "build-22", "7",
]
TOPICS = [
    "disk failure repair storage sector hardware backup volume device drive".split(),
    "release plan deploy rollback version build review branch commit merge".split(),
    "widget cache timeout worker queue request retry response client server".split(),
    "garden flower tree water leaf seed soil plant grass green".split(),
    "archive decision pricing credit budget report meeting note project acme".split(),
]


# Keep syntax in explicit queries. Plain queries also accept the parts of identifiers.
STRICT_QUERY = "illo_search_query('simple',%s) || illo_search_query('english',%s)"
RELAXED_QUERY = " || ".join(
    "(SELECT coalesce(string_agg(quote_literal(lexeme), ' | '), '')::tsquery "
    f"FROM unnest(tsvector_to_array(illo_search_tokens('{config}',%s))) AS lexemes(lexeme))"
    for config in ("simple", "english")
)
HALF_LIFE_DAYS = 21


def utc_now():
    return datetime.now(UTC)


def lexical_query(query):
    """Time words guide age ranking. Quoted text and search operators keep their meaning."""
    if explicit_query(query):
        return query
    return re.sub(r"(?<![\w.@+-])(?:today|yesterday|this\s+week)(?![\w.@+-])", " ", query, flags=re.I).strip()


def explicit_query(query):
    return bool(re.search(r'(?:^|\s)-\S|"|(?:^|\s)(?:OR|or)(?=\s|$)', query))


def query_sql(query):
    relaxed = "''::tsquery" if explicit_query(query) else RELAXED_QUERY
    params = (query, query) if explicit_query(query) else (query,) * 4
    return f"SELECT {STRICT_QUERY} AS strict, {relaxed} AS relaxed", params


# Rank all visible candidates before LIMIT, including partial matches. A strict match
# gets twice the weight. Age has a 21-day half-life and a 20% floor for old decisions.
# RRF adds only a small source-rank bonus later; it cannot erase match strength or age.
RANK_COLUMNS = f"""
    CASE WHEN vector @@ q.strict THEN 'strict' ELSE 'fallback' END AS match_tier,
    ts_rank(ARRAY[0.1,0.5,0.75,1.0]::real[], vector,
      CASE WHEN vector @@ q.strict THEN q.strict ELSE q.relaxed END,32) AS lexical,
    0.2 + 0.8 * power(0.5, floor(greatest(0,extract(epoch FROM (%s::timestamptz-at))) /
      86400) / {HALF_LIFE_DAYS}) AS recency
"""
RANK_ORDER = "lexical * recency * CASE match_tier WHEN 'strict' THEN 2 ELSE 1 END"


async def old_items(conn, principal, query, kinds, limit, now, slack_channels):
    q, params = query_sql(query)
    rows = await all_rows(conn, f"""
        WITH q AS ({q}), texts AS (
          SELECT i.*,p.key AS owner,coalesce(i.source_at,i.created_at) AS at,
            illo_search_vector(i.title,i.summary,i.body) AS vector
          FROM items i LEFT JOIN people p ON p.id=i.owner_person_id
          WHERE (i.visibility='team' OR i.owner_person_id=
            (SELECT id FROM people WHERE 'person:'||key=%s AND active))
            AND (%s::text[] IS NULL OR i.kind=ANY(%s))
            AND (i.kind<>'slack' OR split_part(i.source_ref,':',2)=ANY(%s))
        ), ranked AS (
          SELECT t.*, {RANK_COLUMNS},
            ts_headline('simple',concat_ws(' ',t.summary,t.body),
              CASE WHEN vector @@ q.strict THEN q.strict ELSE q.relaxed END,
              'MaxWords=45,MinWords=15,StartSel="",StopSel=""') AS snippet
          FROM texts t,q WHERE vector @@ q.strict OR vector @@ q.relaxed
        ) SELECT * FROM ranked ORDER BY {RANK_ORDER} DESC,at DESC,id DESC LIMIT %s
    """, (*params, principal, kinds, kinds, list(slack_channels), now, limit))
    return [Hit(
        r["source_ref"], r["kind"], r["source_ref"].split(":", 1)[0], r["title"],
        r["snippet"], first_link(r["links"]), r["owner"], r["at"],
        personal=r["visibility"] == "private", match_tier=r["match_tier"],
        lexical=float(r["lexical"]), recency=float(r["recency"]),
    ) for r in rows]


async def old_lanes(conn, principal, query, kinds, limit, now):
    if kinds is not None and "lane" not in kinds:
        return []
    q, params = query_sql(query)
    rows = await all_rows(conn, f"""
        WITH q AS ({q}), texts AS (
          SELECT l.*,p.key AS owner,concat_ws(' ',l.summary,e.text) AS body,
            l.updated_at AS at,
            illo_search_vector(l.title,'',concat_ws(' ',l.summary,e.text)) AS vector
          FROM lanes l LEFT JOIN people p ON p.id=l.owner_person_id
          LEFT JOIN LATERAL (SELECT string_agg(recent.text,' ') AS text FROM
            (SELECT text FROM lane_events WHERE lane_id=l.id ORDER BY at DESC,id DESC LIMIT 20)
            recent) e ON true
          WHERE l.visibility='team' OR l.owner_person_id=
            (SELECT id FROM people WHERE 'person:'||key=%s AND active)
        ), ranked AS (
          SELECT t.*, {RANK_COLUMNS},
            ts_headline('simple',t.body,
              CASE WHEN vector @@ q.strict THEN q.strict ELSE q.relaxed END,
              'MaxWords=45,MinWords=15,StartSel="",StopSel=""') AS snippet
          FROM texts t,q WHERE vector @@ q.strict OR vector @@ q.relaxed
        ) SELECT * FROM ranked ORDER BY {RANK_ORDER} DESC,at DESC,id DESC LIMIT %s
    """, (*params, principal, now, limit))
    return [Hit(
        f"lane:{r['id']}", "lane", r["source"], r["title"],
        f"State: {r['state']}. {r['snippet']}", first_link(r["links"]), r["owner"], r["at"],
        personal=r["visibility"] == "private", match_tier=r["match_tier"],
        lexical=float(r["lexical"]), recency=float(r["recency"]),
    ) for r in rows]


async def build21_items(conn, principal, query, kinds, limit, now, slack_channels):
    q, params = query_sql(query)
    # Scalar query reads let GIN find matches before vector and rank work starts.
    rows = await all_rows(conn, f"""
        WITH q AS ({q}), texts AS MATERIALIZED (
          SELECT i.*,p.key AS owner,coalesce(i.source_at,i.created_at) AS at,
            illo_search_vector(i.title,i.summary,i.body) AS vector
          FROM items i LEFT JOIN people p ON p.id=i.owner_person_id
          WHERE (i.visibility='team' OR i.owner_person_id=
            (SELECT id FROM people WHERE 'person:'||key=%s AND active))
            AND (%s::text[] IS NULL OR i.kind=ANY(%s))
            AND (i.kind<>'slack' OR split_part(i.source_ref,':',2)=ANY(%s))
            AND (illo_search_vector(i.title,i.summary,i.body) @@ (SELECT strict FROM q)
              OR illo_search_vector(i.title,i.summary,i.body) @@ (SELECT relaxed FROM q))
        ), ranked AS MATERIALIZED (
          SELECT t.*, {RANK_COLUMNS}
          FROM texts t,q
        ), page AS MATERIALIZED (
          SELECT * FROM ranked ORDER BY {RANK_ORDER} DESC,at DESC,id DESC LIMIT %s
        ) SELECT t.*,
            ts_headline('simple',concat_ws(' ',t.summary,t.body),
              CASE WHEN vector @@ q.strict THEN q.strict ELSE q.relaxed END,
              'MaxWords=45,MinWords=15,StartSel="",StopSel=""') AS snippet
          FROM page t,q ORDER BY {RANK_ORDER} DESC,at DESC,id DESC
    """, (*params, principal, kinds, kinds, list(slack_channels), now, limit))
    return [Hit(
        r["source_ref"], r["kind"], r["source_ref"].split(":", 1)[0], r["title"],
        r["snippet"], first_link(r["links"]), r["owner"], r["at"],
        personal=r["visibility"] == "private", match_tier=r["match_tier"],
        lexical=float(r["lexical"]), recency=float(r["recency"]),
    ) for r in rows]


@pytest.fixture
async def corpus(conn, ada, ben, tmp_path):
    # Start before both stored-vector migrations to measure their backfills.
    for name in ("illo_lane_search_vector", "illo_lane_event_search_lock", "illo_lane_event_search_refresh"):
        await conn.execute(f"DROP FUNCTION {name}() CASCADE")
    await conn.execute("DROP TABLE lane_search")
    await conn.execute("DROP INDEX lane_events_recent_idx")
    await conn.execute("DELETE FROM schema_migrations WHERE name='0405_search_lanes.sql'")
    await conn.execute("DROP FUNCTION illo_item_search_vector() CASCADE")
    await conn.execute("DROP FUNCTION illo_slack_event_search_vector() CASCADE")
    await conn.execute("DROP TABLE item_search,slack_event_search")
    await conn.execute("DELETE FROM schema_migrations WHERE name='0406_search_items.sql'")
    rng = random.Random(21021)
    rows = []
    for n in range(5000):
        words = TOPICS[n % len(TOPICS)]
        size = rng.choices([200, 400, 800, 1400, 2200, 4000, 10000], [15, 20, 25, 20, 10, 9, 1])[0]
        body = " ".join(rng.choices(words, k=size))[:size]
        extra = (" common" if n % 20 else "") + (" quartz" if n % 67 == 0 else "")
        if n % 11 == 0:
            extra += " 5.5 build-22 7"
        if n % 29 == 0:
            extra += " acme-backend service.module worker-node mira.orbit@example.com alpha-beta"
        if n % 113 == 0:
            extra += " nebula"
        kind = ("archive", "note", "decision", "saved", "slack")[n % 5]
        ref = f"slack:{CHANNELS[n % 2]}:{n}" if kind == "slack" else f"{kind}:{n}"
        rows.append((kind, ref, " ".join(rng.choices(words, k=5)),
                     " ".join(rng.choices(words, k=25)), body[:size-len(extra)] + extra,
                     ada["id"] if n % 2 else ben["id"],
                     "private" if n % 3 == 0 else "team", NOW - timedelta(days=n % 130 - 2)))
    async with conn.cursor() as cur:
        await cur.executemany("""INSERT INTO items
            (kind,source_ref,title,summary,body,owner_person_id,visibility,source_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""", rows)
    for n in range(500):
        words = TOPICS[n % len(TOPICS)]
        lane = await one(conn, """INSERT INTO lanes
            (source,external_id,title,state,summary,owner_person_id,visibility,updated_at)
            VALUES ('manual',%s,%s,'working',%s,%s,%s,%s) RETURNING id""",
            (str(n), " ".join(rng.choices(words, k=5)), " ".join(rng.choices(words, k=25)),
             ada["id"] if n % 2 else ben["id"], "private" if n % 3 == 0 else "team",
             NOW - timedelta(days=n % 130 - 2)))
        async with conn.cursor() as cur:
            await cur.executemany("""INSERT INTO lane_events (lane_id,kind,text,at)
                VALUES (%s,'report',%s,%s)""", [
                (lane["id"], " ".join(rng.choices(words, k=30)) +
                 (" quartz acme-backend service.module mira.orbit@example.com" if j == 0 else ""),
                 NOW + timedelta(seconds=j // 2)) for j in range(n % 31)
            ])
    await conn.commit()
    from pathlib import Path

    migrations = Path(__file__).parents[2] / "illo/migrations"
    (tmp_path / "0405_search_lanes.sql").write_text((migrations / "0405_search_lanes.sql").read_text())
    started = perf_counter()
    assert await migrate(conn, tmp_path) == ["0405_search_lanes.sql"]
    print(f"\n0405 backfill: {perf_counter()-started:.3f}s; 500 lanes")
    started = perf_counter()
    assert await migrate(conn) == ["0406_search_items.sql"]
    print(f"\n0406 backfill: {perf_counter()-started:.3f}s; 5,000 items")
    assert await migrate(conn) == []
    for table in ("items", "lanes", "lane_events", "lane_search", "item_search"):
        await conn.execute(f"ANALYZE {table}")
    size = await one(conn, "SELECT sum(octet_length(title)+octet_length(summary)+octet_length(body)) AS bytes FROM items")
    print(f"Synthetic item text: {size['bytes']} bytes")
    return conn


def assert_hits(actual, expected):
    assert [h.source_ref for h in actual] == [h.source_ref for h in expected]
    for a, b in zip(actual, expected, strict=True):
        assert a.match_tier == b.match_tier
        assert a.lexical == pytest.approx(b.lexical, abs=1e-6)
        assert a.recency == pytest.approx(b.recency, abs=1e-6)
        assert search._strength(a) == pytest.approx(search._strength(b), abs=1e-6)
        # Snippets and all other fields must also stay identical.
        assert asdict(a) == asdict(b)


async def test_parity_plans_and_speed(corpus, monkeypatch):
    conn = corpus
    # Restore the Build 21 index only for its reference queries and speed comparison.
    await conn.execute("CREATE INDEX items_search_parts_idx ON items USING gin (illo_search_vector(title,summary,body))")
    for principal, kinds, channels, queries in [
        ("person:ada", None, CHANNELS, QUERIES),
        ("team", None, CHANNELS[:1], QUERIES[:5]),
        ("person:ben", ["note", "slack"], CHANNELS[:1], QUERIES[:5]),
        ("person:ben", ["lane"], [], QUERIES[:5]),
    ]:
        for query in queries:
            args = (conn, principal, query, kinds, 50, NOW)
            actual = await search._items(*args, channels)
            assert_hits(actual, await old_items(*args, channels))
            assert_hits(actual, await build21_items(*args, channels))
            assert_hits(await search._lanes(*args), await old_lanes(*args))

    original = search.all_rows
    plans = []

    async def explain(connection, sql, params):
        plan = await original(connection, "EXPLAIN (ANALYZE, BUFFERS) " + sql, params)
        plans.append("\n".join(r["QUERY PLAN"] for r in plan))
        return []

    async with conn.transaction(force_rollback=True):
        with monkeypatch.context() as patch:
            patch.setattr(search, "all_rows", explain)
            await search._items(conn, "person:ada", "quartz release", None, 50, NOW, CHANNELS)
            await search._lanes(conn, "person:ada", "quartz release", None, 50, NOW)
            if (plans[0].count("Bitmap Index Scan on items_search_vector_idx") != 2 or
                    plans[1].count("Bitmap Index Scan on lanes_search_parts_idx") != 2):
                # Small test tables can favor a scan. Check index eligibility in that case.
                plans.clear()
                await conn.execute("SET LOCAL enable_seqscan=off")
                await search._items(conn, "person:ada", "quartz release", None, 50, NOW, CHANNELS)
                await search._lanes(conn, "person:ada", "quartz release", None, 50, NOW)
                print("Small-table plan check: enable_seqscan=off")
        assert plans[0].count("Bitmap Index Scan on items_search_vector_idx") == 2
        assert plans[1].count("Bitmap Index Scan on lanes_search_parts_idx") == 2
    print("\n" + "\n".join(plans))

    # Include the shared search merge. Stub channel proof and disable external providers.
    ctx = SimpleNamespace(github_client=None, workspace=SimpleNamespace(
        config=SimpleNamespace(github=SimpleNamespace(repositories=[]))))

    async def channels(*args):
        return dict.fromkeys(CHANNELS, "acme")

    monkeypatch.setattr(slack_index, "channels", channels)
    monkeypatch.setattr(search, "_providers", {})
    monkeypatch.setattr(search, "utc_now", lambda: NOW)
    # Give the Build 21 reference its original expression index during this comparison.
    async with conn.transaction(force_rollback=True):
        old_times, new_times = [], []
        for _ in range(3):
            started = perf_counter()
            with monkeypatch.context() as patch:
                patch.setattr(search, "_items", build21_items)
                patch.setattr(slack_index, "search", build21_slack)
                await search.search(ctx, "person:ada", "common", conn=conn)
            old_times.append(perf_counter() - started)
            started = perf_counter()
            await search.search(ctx, "person:ada", "common", conn=conn)
            new_times.append(perf_counter() - started)
        before, after = median(old_times), median(new_times)
        print(f"\nWide recall database part: Build21={before:.6f}s stored={after:.6f}s ratio={before/after:.2f}x")
        assert before >= 10 * after
    await conn.execute("DROP INDEX items_search_parts_idx")


async def build21_slack(ctx, conn, query, kinds, limit, now):
    from illo.sources.search import RANK_COLUMNS, RANK_ORDER, Hit, query_sql
    from illo.sources.slack_index import KINDS, VECTOR, VISIBLE, channels, detail

    if kinds is not None and "slack" not in kinds:
        return []
    allowed = await channels(ctx, conn)
    if not allowed:
        return []
    q, params = query_sql(query)
    rows = await all_rows(conn, f"""
        WITH q AS ({q}), texts AS MATERIALIZED (
          SELECT e.*,e.created_at AS at,({VECTOR}) AS vector
          FROM events e WHERE {VISIBLE}
            AND NOT EXISTS (SELECT 1 FROM items i WHERE i.source_ref='slack:'||e.external_id)
            AND ({VECTOR} @@ (SELECT strict FROM q) OR {VECTOR} @@ (SELECT relaxed FROM q))
        ), ranked AS MATERIALIZED (
          SELECT t.*, {RANK_COLUMNS}
          FROM texts t,q
        ), page AS MATERIALIZED (
          SELECT * FROM ranked ORDER BY {RANK_ORDER} DESC,at DESC,id DESC LIMIT %s
        ) SELECT t.*,
            ts_headline('simple',coalesce(t.payload->>'text',''),
              CASE WHEN vector @@ q.strict THEN q.strict ELSE q.relaxed END,
              'MaxWords=45,MinWords=15,StartSel="",StopSel=""') AS snippet
          FROM page t,q ORDER BY {RANK_ORDER} DESC,at DESC,id DESC
    """, (*params, list(KINDS), list(allowed), now, limit))
    return [Hit(f"slack-event:{row['id']}", "slack", "slack", detail(row, allowed)["title"],
                row["snippet"], at=row["created_at"], match_tier=row["match_tier"],
                lexical=float(row["lexical"]), recency=float(row["recency"])) for row in rows]


async def old_slack(ctx, conn, query, kinds, limit, now):
    from illo.sources.search import RANK_COLUMNS, RANK_ORDER, Hit, query_sql

    if kinds is not None and "slack" not in kinds:
        return []
    allowed = await slack_index.channels(ctx, conn)
    if not allowed:
        return []
    q, params = query_sql(query)
    rows = await all_rows(conn, f"""
        WITH q AS ({q}), texts AS (
          SELECT e.*,e.created_at AS at,({slack_index.VECTOR}) AS vector
          FROM events e WHERE {slack_index.VISIBLE}
            AND NOT EXISTS (SELECT 1 FROM items i WHERE i.source_ref='slack:'||e.external_id)
        ), ranked AS (
          SELECT t.*, {RANK_COLUMNS},
            ts_headline('simple',coalesce(t.payload->>'text',''),
              CASE WHEN vector @@ q.strict THEN q.strict ELSE q.relaxed END,
              'MaxWords=45,MinWords=15,StartSel="",StopSel=""') AS snippet
          FROM texts t,q WHERE vector @@ q.strict OR vector @@ q.relaxed
        ) SELECT * FROM ranked ORDER BY {RANK_ORDER} DESC,at DESC,id DESC LIMIT %s
    """, (*params, list(slack_index.KINDS), list(allowed), now, limit))
    return [Hit(f"slack-event:{row['id']}", "slack", "slack", slack_index.detail(row, allowed)["title"],
                row["snippet"], at=row["created_at"], match_tier=row["match_tier"],
                lexical=float(row["lexical"]), recency=float(row["recency"])) for row in rows]


async def test_slack_parity_and_index_plan(conn, monkeypatch):
    from psycopg.types.json import Jsonb

    async def channels(*args):
        return {"C_ACME": "acme"}

    monkeypatch.setattr(slack_index, "channels", channels)
    for n in range(40):
        await conn.execute("""INSERT INTO events (channel,external_id,kind,principal,payload)
            VALUES ('slack',%s,'message','team',%s)""", (f"C_ACME:{n}", Jsonb({
                "text": "quartz release plan service.module" if n % 2 else "disk failure worker-node",
                "payload": {"channel_id": "C_ACME"},
            })))
    await conn.execute("ANALYZE events")
    for query in QUERIES:
        actual = await slack_index.search(None, conn, query, None, 10, NOW)
        assert_hits(actual, await old_slack(None, conn, query, None, 10, NOW))
        assert_hits(actual, await build21_slack(None, conn, query, None, 10, NOW))
    original = slack_index.all_rows
    plans = []

    async def explain(connection, sql, params):
        plans.extend(await original(connection, "EXPLAIN (ANALYZE) " + sql, params))
        return []

    async with conn.transaction(force_rollback=True):
        await conn.execute("SET LOCAL enable_seqscan=off")
        monkeypatch.setattr(slack_index, "all_rows", explain)
        await slack_index.search(None, conn, "quartz release", None, 10, NOW)
    # A tiny event table can favor the vector primary key after the channel filter.
    assert "slack_event_search" in str(plans)
    assert "illo_search_vector(" not in str(plans)


async def test_ledger_fallback_matches_its_index(conn, monkeypatch):
    from illo.ledger import items

    await items.upsert(conn, source_ref="note:widget", title="Widget cache", body="release plan acme-backend")
    await items.upsert(conn, source_ref="note:disk", title="Disk repair", body="disk failure service.module")
    for query in QUERIES:
        assert await items.search(conn, query) == await old_ledger_items(conn, query)
    original = items.all_rows
    plans = []

    async def explain(connection, sql, params):
        plans.extend(await original(connection, "EXPLAIN (ANALYZE) " + sql, params))
        return []

    async with conn.transaction(force_rollback=True):
        await conn.execute("SET LOCAL enable_seqscan=off")
        monkeypatch.setattr(items, "all_rows", explain)
        await items.search(conn, "widget")
    assert "items_search_vector_idx" in str(plans)


async def old_ledger_items(
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
        WITH q AS (SELECT illo_search_query('simple',%s) || illo_search_query('english',%s) AS query)
        SELECT i.id,i.kind,i.source_ref,i.title,i.summary,i.links,i.source_at,ts_rank(illo_search_vector(i.title,i.summary,i.body),q.query) AS score
        FROM items i,q WHERE illo_search_vector(i.title,i.summary,i.body) @@ q.query
          AND (visibility='team' OR owner_person_id=(SELECT id FROM people WHERE 'person:' || key=%s AND active))
          AND (%s::text[] IS NULL OR kind=ANY(%s)) ORDER BY score DESC,i.id DESC LIMIT %s
    """,
        (query, query, principal, kinds, kinds, max(1, min(limit, 50))),
    )


async def test_item_migration_with_concurrent_writer(corpus, pool):
    import asyncio
    from pathlib import Path

    from psycopg.types.json import Jsonb

    from illo.ledger import items

    conn = corpus
    await conn.execute("DROP FUNCTION illo_item_search_vector() CASCADE")
    await conn.execute("DROP FUNCTION illo_slack_event_search_vector() CASCADE")
    await conn.execute("DROP TABLE item_search,slack_event_search")
    await conn.execute("DELETE FROM schema_migrations WHERE name='0406_search_items.sql'")
    for n in range(4):
        await conn.execute("""INSERT INTO events(channel,external_id,kind,principal,payload)
            VALUES ('slack',%s,'message','team',%s)""",
            (f"C_ACME:{n}", Jsonb({"text": "oldword", "payload": {"channel_id": "C_ACME"}})))
    await conn.commit()
    migration = (Path(__file__).parents[2] / "illo/migrations/0406_search_items.sql").read_text()
    prefix, suffix = migration.split("LOCK TABLE items,events IN SHARE ROW EXCLUSIVE MODE;")
    async with pool.connection() as writer:
        async with conn.transaction():
            started = perf_counter()
            await conn.execute(prefix)
            print(f"\nUnlocked 0406 backfill and indexes: {perf_counter()-started:.6f}s")
            locks = await all_rows(conn, """SELECT mode FROM pg_locks l JOIN pg_class c ON c.oid=l.relation
                WHERE pid=pg_backend_pid() AND c.relname IN ('items','events')""")
            assert {r["mode"] for r in locks} <= {"AccessShareLock"}
            started = perf_counter()
            async with asyncio.timeout(2):
                # These writes use the old schema while the migration transaction stays open.
                await items.upsert(writer, source_ref="saved:concurrent", title="newword", body="newword")
                for field, ref in [("title", "note:1"), ("summary", "decision:2"), ("body", "saved:3")]:
                    await writer.execute(f"UPDATE items SET {field}='newword' WHERE source_ref=%s", (ref,))
                await writer.execute("DELETE FROM items WHERE source_ref='slack:C_ACME:4'")
                await writer.execute("UPDATE events SET payload=jsonb_set(payload,'{text}',%s) WHERE external_id='C_ACME:0'",
                                     (Jsonb("newword"),))
                await writer.execute("DELETE FROM events WHERE external_id='C_ACME:1'")
                await writer.execute("UPDATE events SET principal='person:ada' WHERE external_id='C_ACME:2'")
                await writer.execute("""INSERT INTO events(channel,external_id,kind,principal,payload)
                    VALUES ('slack','C_ACME:4','channel','team',%s)""",
                    (Jsonb({"text": "newword", "payload": {"channel_id": "C_ACME"}}),))
                assert await old_ledger_items(writer, "newword")
                await writer.commit()
            print(f"Concurrent old writer and search: {perf_counter()-started:.6f}s")
            started = perf_counter()
            await conn.execute("LOCK TABLE items,events IN SHARE ROW EXCLUSIVE MODE;" + suffix)
            await conn.execute("INSERT INTO schema_migrations(name) VALUES ('0406_search_items.sql')")
        print(f"0406 writer lock through commit: {perf_counter()-started:.6f}s")
    assert (await one(conn, """SELECT count(*) AS n FROM items i LEFT JOIN item_search s ON s.item_id=i.id
        WHERE s.vector IS DISTINCT FROM illo_search_vector(i.title,i.summary,i.body)"""))["n"] == 0
    assert (await one(conn, "SELECT count(*) AS n FROM item_search"))["n"] == 5000
    assert (await one(conn, "SELECT count(*) AS n FROM slack_event_search"))["n"] == 3
    assert (await one(conn, """SELECT count(*) AS n FROM events e JOIN slack_event_search s ON s.event_id=e.id
        WHERE s.vector<>illo_search_vector('','',coalesce(e.payload->>'text',''))"""))["n"] == 0
