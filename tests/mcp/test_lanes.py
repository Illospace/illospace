import json

import pytest

from illo.db import all_rows, one
from tests.mcp.client import AgentClient


async def test_lane_upsert_events_and_closing_summary(app, http):
    agent = await AgentClient.create(app, http)
    lane = {
        "source": "t3",
        "external_id": "acme-1",
        "title": "Cobalt checks",
        "state": "working",
        "summary": "The preview checks run.",
        "repo": "Acme/widget",
        "links": ["https://example.com/acme-1"],
    }
    first = await agent.call("lane_report", lane)
    again = await agent.call("lane_report", {"source": "t3", "external_id": "acme-1"})
    assert again == first
    changed = await agent.call(
        "lane_report", {**lane, "state": "review", "event": "The diff is ready."}
    )
    assert changed["lane_id"] == first["lane_id"] and changed["state"] == "review"
    async with app.state.ctx.pool.connection() as conn:
        rows = await all_rows(conn, "SELECT * FROM lane_events ORDER BY id")
        assert len(rows) == 2 and rows[-1]["text"] == "The diff is ready."
        stored = await one(conn, "SELECT * FROM lanes")
        assert stored["repo"] == "Acme/widget" and stored["owner_person_id"] == agent.person_id
        assert stored["last_signal_at"]
    closed = await agent.call(
        "lane_report",
        {
            "source": "t3",
            "external_id": "acme-1",
            "state": "done",
            "end_summary": "Cobalt checks pass.",
        },
    )
    await agent.call("lane_report", {"source": "t3", "external_id": "acme-1", "state": "done"})
    async with app.state.ctx.pool.connection() as conn:
        stored = await one(conn, "SELECT * FROM lanes")
        summary = await one(conn, "SELECT * FROM items WHERE kind='lane_summary'")
        assert stored["closed_at"] and closed["state"] == "done"
        assert summary["source_ref"] == f"lane:{first['lane_id']}:end"
        assert summary["kind"] == "lane_summary" and summary["body"] == "Cobalt checks pass."
        assert (await one(conn, "SELECT count(*) AS n FROM items WHERE kind='lane_summary'"))[
            "n"
        ] == 1
    results = await agent.call("lane_search", {"query": "Cobalt", "owner": "ada", "state": "done"})
    assert len(results["lanes"]) == 1

    await agent.call("lane_report", {
        "source": "t3", "external_id": "acme-1", "state": "done", "event": "Acme verified the result.",
    })
    async with app.state.ctx.pool.connection() as conn:
        assert await one(conn, "SELECT kind,text FROM lane_events ORDER BY id DESC LIMIT 1") == {
            "kind": "report", "text": "Acme verified the result.",
        }


async def test_refresh_skips_writes_for_unchanged_closed_lanes(app, http):
    agent = await AgentClient.create(app, http)
    closed = [
        {"source": "t3", "external_id": f"acme-{i}", "state": "done"}
        for i in range(1002)
    ]
    closed[0].update(
        links=[{"url": "https://example.com/acme/1"}],
        progress={"steps_done": 1, "steps_total": 1},
        end_summary="The checks pass.",
    )
    changed = closed[-1]
    working = {"source": "t3", "external_id": "acme-working", "state": "working"}

    async def report(snapshots):
        results = []
        for start in range(0, len(snapshots), 500):
            batch = await agent.call("lane_report", {"lanes": snapshots[start:start + 500]})
            results.extend(batch["lanes"])
        return results

    async def snapshot():
        async with app.state.ctx.pool.connection() as conn:
            rows = await all_rows(conn, """SELECT l.external_id,l.xmin::text AS lane_xmin,
                l.updated_at,l.last_signal_at,s.xmin::text AS search_xmin,s.vector::text
                FROM lanes l JOIN lane_search s ON s.lane_id=l.id""")
            summary = await one(conn, "SELECT xmin::text,source_at FROM items WHERE kind='lane_summary'")
            events = await one(conn, "SELECT count(*) AS n FROM lane_events")
        return {row["external_id"]: row for row in rows}, summary, events

    first = await report([*closed, working])
    before, summary_before, events_before = await snapshot()
    assert len(before) == 1003 and events_before == {"n": 1003}
    assert summary_before is not None

    assert await report([*closed[:-1], {**changed, "summary": "The Acme check passes."}, working]) == first
    after, summary_after, events_after = await snapshot()
    assert summary_after == summary_before
    assert events_after == events_before

    for key in ("lane_xmin", "updated_at", "last_signal_at", "search_xmin", "vector"):
        assert after[changed["external_id"]][key] != before[changed["external_id"]][key]
    assert after[working["external_id"]]["last_signal_at"] > before[working["external_id"]]["last_signal_at"]
    for lane in (changed, working):
        del before[lane["external_id"]], after[lane["external_id"]]
    assert len(after) == 1001 and after == before


@pytest.mark.parametrize("owner", ["ben", "ACME-BEN-EXAMPLE", "ben@example.com", "U_ACME_BEN"])
async def test_owner_mapping(app, http, owner):
    agent = await AgentClient.create(app, http)
    await agent.call("lane_report", {"source": "codex", "external_id": "acme-1", "owner": owner})
    async with app.state.ctx.pool.connection() as conn:
        lane = await one(
            conn, "SELECT l.*,p.key FROM lanes l JOIN people p ON p.id=l.owner_person_id"
        )
        assert lane["key"] == "ben"


async def test_batch_and_empty_collector_liveness(app, http):
    agent = await AgentClient.create(app, http)
    result = await agent.call(
        "lane_report",
        {
            "lanes": [
                {"source": "t3", "external_id": "acme-1", "state": "working"},
                {"source": "claude-code", "external_id": "acme-2", "state": "blocked"},
            ],
            "collector": {"label": "acme-laptop", "version": "2", "threads_seen": 2},
        },
    )
    assert len(result["lanes"]) == 2
    await agent.call(
        "lane_report",
        {"lanes": [], "collector": {"label": "acme-laptop", "version": "3", "threads_seen": 0}},
    )
    async with app.state.ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM sensors WHERE key='collector:acme-laptop'")
        assert row["last_ok_at"] and json.loads(row["detail"])["version"] == "3"
        assert json.loads(row["detail"])["threads_seen"] == 0


async def test_batch_failure_rolls_back_all_writes_but_keeps_audit(app, http):
    agent = await AgentClient.create(app, http)
    await agent.call(
        "lane_report",
        {
            "lanes": [
                {"source": "t3", "external_id": "acme-1"},
                {"source": "t3", "external_id": "acme-2", "owner": "missing"},
            ]
        },
        error=True,
    )
    async with app.state.ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT count(*) AS n FROM lanes"))["n"] == 0
        assert (await one(conn, "SELECT decision FROM audit"))["decision"] == "deny"


async def test_lane_events_and_stale_are_searchable(app, http):
    agent = await AgentClient.create(app, http)
    await agent.call(
        "lane_report",
        {
            "source": "t3",
            "external_id": "acme-1",
            "state": "working",
            "event": "The zephyr migration started.",
        },
    )
    results = await agent.call(
        "illo_read", {"capability": "knowledge.search", "arguments": {"query": "zephyr"}}
    )
    assert results["results"][0]["source_ref"].startswith("lane:")
    async with app.state.ctx.pool.connection() as conn:
        await conn.execute("UPDATE lanes SET last_signal_at=now()-interval '2 hours'")
    found = await agent.call("lane_search", {"state": "stale"})
    assert len(found["lanes"]) == 1 and found["lanes"][0]["state"] == "working"


async def test_stale_filter_runs_before_the_limit(app, http):
    agent = await AgentClient.create(app, http)
    await agent.call("lane_report", {"source": "t3", "external_id": "acme-old", "state": "working"})
    async with app.state.ctx.pool.connection() as conn:
        await conn.execute("UPDATE lanes SET last_signal_at=now()-interval '2 hours'")
    await agent.call("lane_report", {"source": "t3", "external_id": "acme-new", "state": "working"})
    found = await agent.call("lane_search", {"state": "stale", "limit": 1})
    assert len(found["lanes"]) == 1 and found["lanes"][0]["external_id"] == "acme-old"


async def test_open_lane_report_search_and_stale_filter(app, http):
    agent = await AgentClient.create(app, http)
    row = await agent.call(
        "lane_report",
        {"source": "t3", "external_id": "backlog", "title": "Acme backlog", "state": "open"},
    )
    assert row["state"] == "open"
    async with app.state.ctx.pool.connection() as conn:
        await conn.execute("UPDATE lanes SET last_signal_at=now()-interval '10 days'")
    assert (await agent.call("lane_search"))["lanes"] == []
    assert (await agent.call("lane_search", {"state": "stale"}))["lanes"] == []
    for args in ({"state": "open"}, {"query": "Acme"}):
        found = (await agent.call("lane_search", args))["lanes"]
        assert len(found) == 1 and found[0]["state"] == "open" and not found[0]["stale"]


async def test_step_fields_are_optional_and_preserved(app, http):
    agent = await AgentClient.create(app, http)
    ref = {"source": "t3", "external_id": "step-a"}
    result = await agent.call("lane_report", {**ref, "is_step": True, "parent_ref": "parent-a"})
    await agent.call("lane_report", ref)
    old = await agent.call("lane_report", {"source": "codex", "external_id": "old-a"})
    async with app.state.ctx.pool.connection() as conn:
        step = await one(conn, "SELECT * FROM lanes WHERE id=%s", (result["lane_id"],))
        assert step["is_step"] and step["parent_ref"] == "parent-a"
        lane = await one(conn, "SELECT * FROM lanes WHERE id=%s", (old["lane_id"],))
        assert not lane["is_step"] and lane["parent_ref"] is None
    await agent.call("lane_report", {**ref, "is_step": False, "parent_ref": None})
    async with app.state.ctx.pool.connection() as conn:
        step = await one(conn, "SELECT * FROM lanes WHERE id=%s", (result["lane_id"],))
        assert not step["is_step"] and step["parent_ref"] is None


@pytest.mark.parametrize("state", ["review", "working"])
async def test_reopened_lane_clears_closure_and_saved_summary(app, http, state):
    from illo.asks.service import is_live
    from illo.ledger import ask_workflow, asks

    agent = await AgentClient.create(app, http)
    ref = {"source": "t3", "external_id": "thread-a"}
    result = await agent.call("lane_report", {
        **ref, "title": "Check the plan", "state": "done", "end_summary": "The work finished.",
    })
    await agent.call("lane_report", {**ref, "state": state, "summary": "Please check the plan."})
    async with app.state.ctx.pool.connection() as conn:
        lane = await one(conn, "SELECT * FROM lanes WHERE id=%s", (result["lane_id"],))
        assert lane["state"] == state
        assert lane["closed_at"] is None and lane["end_summary"] is None
        await ask_workflow.record_decisions(conn)
        assert await one(conn, "SELECT * FROM items WHERE source_ref=%s",
                         (f"lane:{lane['id']}:end",)) is None
        ask = await asks.create(conn, lane_id=lane["id"], owner_person_id=lane["owner_person_id"],
                                question="Which check comes next?", source_ref="t3:thread-a:request-a")
        assert await is_live(conn, ask)


async def test_lane_search_vector_tracks_reports_and_last_twenty_events(app, http):
    agent = await AgentClient.create(app, http)
    ref = {"source": "t3", "external_id": "search-freshness"}
    row = await agent.call("lane_report", {**ref, "title": "Widget", "event": "quartz"})

    async def found(query):
        result = await agent.call("illo_read", {
            "capability": "knowledge.search", "arguments": {"query": query, "kinds": ["lane"]},
        })
        return [hit["source_ref"] for hit in result["results"]]

    assert await found("quartz") == [f"lane:{row['lane_id']}"]
    await agent.call("lane_report", {**ref, "summary": "nebula"})
    assert await found("nebula") == [f"lane:{row['lane_id']}"]
    await agent.call("lane_report", {**ref, "summary": "garden", "title": "Newtitle"})
    assert await found("nebula") == []
    assert await found("newtitle") == [f"lane:{row['lane_id']}"]
    for n in range(20):
        await agent.call("lane_report", {**ref, "event": f"Progress step {n}"})
    assert await found("quartz") == []
    async with app.state.ctx.pool.connection() as conn:
        # Equal event times use the event ID as the final order key.
        await conn.execute("UPDATE lane_events SET at='2026-01-01' WHERE lane_id=%s", (row["lane_id"],))
    assert await found("quartz") == []
