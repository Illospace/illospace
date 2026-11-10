import pytest

from illo.db import one
from illo.ledger import asks
from tests.mcp.client import AgentClient
from tests.mcp.test_asks import ASK


@pytest.mark.parametrize(
    "constraints",
    [
        {"visibility": "private"},
        {"privacy": "private"},
        {"private": True},
        {"privacy": {"owner_only": True}},
    ],
)
async def test_rule_03_private_save_is_visible_only_to_its_owner(app, http, constraints):
    owner = await AgentClient.create(app, http)
    rotated = await AgentClient.create(app, http, label="acme-desktop")
    other = await AgentClient.create(app, http, person="ben")
    saved = await owner.call(
        "illo_submit",
        {
            "message": "The private cobalt decision.",
            "desired_outcome": "preserve_knowledge",
            "constraints": constraints,
        },
    )
    ref = saved["ilo_outcome"]["handle"]
    assert saved["mutated_target_refs"][0]["visibility"] == "private"
    for agent, visible in ((owner, True), (rotated, True), (other, False)):
        found = await agent.call(
            "illo_read", {"capability": "knowledge.search", "arguments": {"query": "cobalt"}}
        )
        assert bool(found["results"]) is visible
        full = await agent.call(
            "illo_read", {"capability": "knowledge.get", "arguments": {"source_ref": ref}}
        )
        assert bool(full["items"]) is visible
        if not visible:
            missing = await agent.call(
                "illo_read",
                {"capability": "knowledge.get", "arguments": {"source_ref": "saved:999999"}},
            )
            assert full.keys() == missing.keys() and "body" not in full
    await other.call("illo_get_result", {"event_id": saved["event_id"]}, error=True)
    result = await rotated.call("illo_get_result", {"event_id": saved["event_id"]})
    assert result["evidence_status"] == "satisfied"


async def test_rule_03_private_lane_cannot_be_read_or_overwritten(app, http):
    owner = await AgentClient.create(app, http)
    other = await AgentClient.create(app, http, person="ben")
    args = {
        "source": "t3",
        "external_id": "acme-private-1",
        "visibility": "private",
        "title": "Private zephyr checks",
        "state": "done",
        "end_summary": "Private zephyr summary.",
    }
    lane = await owner.call("lane_report", args)
    await other.call(
        "lane_report",
        {"source": "t3", "external_id": "acme-private-1", "state": "working"},
        error=True,
    )
    await other.call(
        "ask_create", {**ASK, "lane": {"source": "t3", "external_id": "acme-private-1"}}, error=True
    )
    assert (await other.call("lane_search", {"query": "zephyr"}))["lanes"] == []
    assert (
        await other.call(
            "illo_read", {"capability": "knowledge.search", "arguments": {"query": "zephyr"}}
        )
    )["results"] == []
    assert (
        await other.call(
            "illo_read",
            {
                "capability": "knowledge.get",
                "arguments": {"source_ref": f"lane:{lane['lane_id']}:end"},
            },
        )
    )["items"] == []
    await owner.call(
        "lane_report",
        {"source": "t3", "external_id": "acme-bad", "visibility": "private", "owner": "ben"},
        error=True,
    )


async def test_rule_03_summary_becomes_private_with_its_lane(app, http):
    owner = await AgentClient.create(app, http)
    other = await AgentClient.create(app, http, person="ben")
    lane = await owner.call(
        "lane_report",
        {"source": "t3", "external_id": "acme-1", "state": "done", "end_summary": "Cobalt result."},
    )
    await owner.call(
        "lane_report",
        {"source": "t3", "external_id": "acme-1", "state": "working", "visibility": "private"},
    )
    result = await other.call(
        "illo_read",
        {"capability": "knowledge.get", "arguments": {"source_ref": f"lane:{lane['lane_id']}:end"}},
    )
    assert result["items"] == []


async def test_rule_01_client_cannot_override_the_principal(app, http):
    agent = await AgentClient.create(app, http)
    for name, args in (
        ("illo_submit", {"message": "A request.", "principal": "person:ben"}),
        ("ask_create", {**ASK, "principal": "person:ben"}),
        ("lane_search", {"principal": "person:ben"}),
    ):
        await agent.call(name, args, error=True)
    async with app.state.ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT count(*) AS n FROM runs"))["n"] == 0


async def test_rule_07_other_person_cannot_pull_ack_or_cancel_an_answer(app, http):
    owner = await AgentClient.create(app, http)
    # Matching labels cannot grant authority across people.
    other = await AgentClient.create(app, http, person="ben")
    created = await owner.call("ask_create", ASK)
    async with app.state.ctx.pool.connection() as conn:
        await asks.answer(conn, created["ask_id"], person_id=owner.person_id, option="preview")
    for scope in ("token", "person"):
        assert (await other.call("ask_pull", {"scope": scope}))["answers"] == []
    await other.call("ask_cancel", {"ask_id": created["ask_id"]}, error=True)
    await other.call("ask_cancel", {"source_ref": ASK["source_ref"]}, error=True)
    await owner.call("ask_pull")
    await other.call("ask_ack", {"ask_id": created["ask_id"]}, error=True)
    async with app.state.ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT delivery_state FROM asks"))["delivery_state"] == "delivered"
