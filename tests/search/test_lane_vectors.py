"""All lane writers share the database vector maintenance rules."""

import asyncio

from illo.db import one
from illo.ledger import lanes


async def assert_fresh(conn):
    row = await one(conn, """SELECT bool_and(s.vector=illo_search_vector(l.title,'',
        concat_ws(' ',l.summary,(SELECT string_agg(recent.text,' ') FROM
          (SELECT text FROM lane_events WHERE lane_id=l.id ORDER BY at DESC,id DESC LIMIT 20) recent)))) AS fresh
        FROM lanes l JOIN lane_search s ON s.lane_id=l.id""")
    assert row["fresh"]


async def test_event_edits_moves_and_deletes_refresh_vectors(conn):
    a = await lanes.upsert(conn, source="manual", external_id="a", title="Widget")
    b = await lanes.upsert(conn, source="manual", external_id="b", title="Garden")
    event = await lanes.add_event(conn, a["id"], "report", "quartz")
    await assert_fresh(conn)
    await conn.execute("UPDATE lane_events SET text='nebula',lane_id=%s WHERE id=%s", (b["id"], event["id"]))
    await assert_fresh(conn)
    await conn.execute("DELETE FROM lane_events WHERE id=%s", (event["id"],))
    await assert_fresh(conn)


async def test_concurrent_event_and_summary_writers_keep_vector_fresh(conn, pool):
    lane = await lanes.upsert(conn, source="manual", external_id="a", title="Widget")
    await conn.commit()
    ready = asyncio.Event()

    async def report():
        async with pool.connection() as other:
            ready.set()
            await other.execute("INSERT INTO lane_events (lane_id,kind,text) VALUES (%s,'report','quartz')", (lane["id"],))

    async with conn.transaction():
        await conn.execute("UPDATE lanes SET summary='nebula' WHERE id=%s", (lane["id"],))
        task = asyncio.create_task(report())
        await ready.wait()
    async with asyncio.timeout(2):
        await task
    await assert_fresh(conn)
