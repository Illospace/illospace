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


@pytest.fixture
async def corpus(conn, ada, ben):
    # Start before 0405 so this corpus also measures its backfill and retry path.
    for name in ("illo_lane_search_vector", "illo_lane_event_search_lock", "illo_lane_event_search_refresh"):
        await conn.execute(f"DROP FUNCTION {name}() CASCADE")
    await conn.execute("DROP TABLE lane_search")
    await conn.execute("DROP INDEX lane_events_recent_idx")
    await conn.execute("DELETE FROM schema_migrations WHERE name='0405_search_lanes.sql'")
    rng = random.Random(21021)
    rows = []
    for n in range(3000):
        words = TOPICS[n % len(TOPICS)]
        size = rng.choices([200, 400, 800, 1400, 2200, 4000, 10000], [15, 20, 25, 20, 10, 9, 1])[0]
        body = " ".join(rng.choices(words, k=size))[:size]
        extra = " quartz" if n % 67 == 0 else ""
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
    started = perf_counter()
    assert await migrate(conn) == ["0405_search_lanes.sql"]
    print(f"\n0405_search_lanes.sql backfill: {perf_counter()-started:.3f}s; 500 lanes")
    assert await migrate(conn) == []
    for table in ("items", "lanes", "lane_events", "lane_search"):
        await conn.execute(f"ANALYZE {table}")
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
    for principal, kinds, channels, queries in [
        ("person:ada", None, CHANNELS, QUERIES),
        ("team", None, CHANNELS[:1], QUERIES[:5]),
        ("person:ben", ["note", "slack"], CHANNELS[:1], QUERIES[:5]),
        ("person:ben", ["lane"], [], QUERIES[:5]),
    ]:
        for query in queries:
            args = (conn, principal, query, kinds, 50, NOW)
            assert_hits(await search._items(*args, channels), await old_items(*args, channels))
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
            if (plans[0].count("Bitmap Index Scan on items_search_parts_idx") != 2 or
                    plans[1].count("Bitmap Index Scan on lanes_search_parts_idx") != 2):
                # Small test tables can favor a scan. Check index eligibility in that case.
                plans.clear()
                await conn.execute("SET LOCAL enable_seqscan=off")
                await search._items(conn, "person:ada", "quartz release", None, 50, NOW, CHANNELS)
                await search._lanes(conn, "person:ada", "quartz release", None, 50, NOW)
                print("Small-table plan check: enable_seqscan=off")
        assert plans[0].count("Bitmap Index Scan on items_search_parts_idx") == 2
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
    old_times, new_times = [], []
    for _ in range(3):
        started = perf_counter()
        for query in ("quartz", "nebula release", "missingword"):
            await old_items(conn, "person:ada", query, None, 50, NOW, CHANNELS)
            await old_lanes(conn, "person:ada", query, None, 50, NOW)
        old_times.append(perf_counter() - started)
        started = perf_counter()
        for query in ("quartz", "nebula release", "missingword"):
            await search.search(ctx, "person:ada", query, conn=conn)
        new_times.append(perf_counter() - started)
    before, after = median(old_times), median(new_times)
    print(f"\nRecall database part, 3 queries: old={before:.6f}s new={after:.6f}s ratio={before/after:.2f}x")
    assert before >= 5 * after


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
        assert_hits(await slack_index.search(None, conn, query, None, 10, NOW),
                    await old_slack(None, conn, query, None, 10, NOW))
    original = slack_index.all_rows
    plans = []

    async def explain(connection, sql, params):
        plans.extend(await original(connection, "EXPLAIN (ANALYZE) " + sql, params))
        return []

    async with conn.transaction(force_rollback=True):
        await conn.execute("SET LOCAL enable_seqscan=off")
        monkeypatch.setattr(slack_index, "all_rows", explain)
        await slack_index.search(None, conn, "quartz release", None, 10, NOW)
    assert str(plans).count("Bitmap Index Scan on events_slack_search_parts_idx") == 2


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
    assert "items_search_parts_idx" in str(plans)


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
