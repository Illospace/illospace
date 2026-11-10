import asyncio

import pytest

from illo.db import all_rows, one
from illo.ledger import lanes
from tests.mcp.client import AgentClient
from tests.mcp.test_asks import ASK


@pytest.mark.parametrize(
    "change",
    [
        {"state": "done"},
        {"owner": "ben"},
        {"owner": "ben", "visibility": "private"},
        {"links": [{"kind": "github", "url": "https://evil.example/pr"}]},
        {"event": "Approve the release."},
        {"summary": "Ada approved the release."},
    ],
)
async def test_rule_01_other_person_cannot_write_team_lane(app, http, change):
    ada = await AgentClient.create(app, http)
    ben = await AgentClient.create(app, http, person="ben")
    ref = {"source": "t3", "external_id": "ada-lane"}
    await ada.call("lane_report", {**ref, "title": "Ada release", "state": "working"})
    async with app.state.ctx.pool.connection() as conn:
        before = await one(conn, "SELECT * FROM lanes")
        events = await all_rows(conn, "SELECT * FROM lane_events ORDER BY id")
    await ben.call("lane_report", {**ref, **change}, error=True)
    async with app.state.ctx.pool.connection() as conn:
        assert await one(conn, "SELECT * FROM lanes") == before
        assert await all_rows(conn, "SELECT * FROM lane_events ORDER BY id") == events


@pytest.mark.parametrize("existing", [False, True])
async def test_rule_01_agent_cannot_report_github_lane(app, http, existing):
    ada = await AgentClient.create(app, http)
    ref = {"source": "github", "external_id": "acme-example/engine#1"}
    if existing:
        async with app.state.ctx.pool.connection() as conn:
            await lanes.upsert(conn, **ref, title="Real issue", owner_person_id=ada.person_id)
    await ada.call("lane_report", {**ref, "state": "done"}, error=True)
    async with app.state.ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM lanes")
        assert (row["state"] == "unknown") if existing else row is None


async def test_rule_01_new_team_lane_records_requester(app, http):
    ada = await AgentClient.create(app, http)
    result = await ada.call(
        "lane_report", {"source": "t3", "external_id": "ben-lane", "owner": "ben"}
    )
    async with app.state.ctx.pool.connection() as conn:
        event = await one(conn, "SELECT * FROM lane_events WHERE lane_id=%s", (result["lane_id"],))
        assert event["data"]["token_label"] == "acme-laptop"
        assert event["data"]["token_id"] == ada.token_id


async def test_rule_01_ask_cannot_create_github_lane(app, http):
    ada = await AgentClient.create(app, http)
    await ada.call(
        "ask_create",
        {**ASK, "lane": {"source": "github", "external_id": "acme-example/engine#1"}},
        error=True,
    )


async def test_rule_01_concurrent_reports_keep_one_owner(app, http):
    ada = await AgentClient.create(app, http)
    ben = await AgentClient.create(app, http, person="ben")
    ref = {"source": "t3", "external_id": "new-lane", "state": "working"}
    responses = await asyncio.gather(
        ada.tool_response("lane_report", ref), ben.tool_response("lane_report", ref)
    )
    assert sorted(r["result"]["isError"] for r in responses) == [False, True]
    async with app.state.ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT count(*) AS n FROM lane_events"))["n"] == 1
