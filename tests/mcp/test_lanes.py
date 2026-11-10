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
