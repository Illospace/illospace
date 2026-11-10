"""Rule 3: private asks, rules, and digests restrict later writes."""

import pytest

from illo.db import one
from illo.ledger import asks, lanes, people, rules


@pytest.mark.parametrize("tool", ["asks_get", "asks_list", "rules_list", "asks_digest"])
async def test_rule_03_private_read_requires_approval_for_shared_write(app, rpc, rpc_token, tool):
    ctx = app.state.ctx
    run, token = rpc_token
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        lane = await lanes.upsert(
            conn,
            source="t3",
            external_id="private",
            title="Salary review",
            owner_person_id=ada["id"],
            visibility="private",
        )
        ask = await asks.create(
            conn,
            owner_person_id=ada["id"],
            lane_id=lane["id"],
            question="Approve the raise?",
            proof={"text": "Private HR note"},
        )
        await rules.create(conn, owner_person_id=ada["id"], text="Private salary rule")
    response = await rpc(
        token,
        "tools/call",
        {"name": tool, "arguments": {"id": ask["id"]} if tool == "asks_get" else {}},
    )
    result = response.json()["result"]["structuredContent"]
    assert result["decision"] == "allow"
    async with ctx.pool.connection() as conn:
        stored = await one(conn, "SELECT * FROM runs WHERE id=%s", (run["id"],))
        assert stored["personal_read"] is True
    response = await rpc(
        token,
        "tools/call",
        {
            "name": "slack_post_channel",
            "arguments": {"channel": "C_ACME_TEAM", "text": "Private HR note"},
        },
    )
    assert response.json()["result"]["structuredContent"]["decision"] == "draft"


@pytest.mark.parametrize("tool", ["asks_get", "asks_list", "rules_list"])
async def test_rule_03_team_read_does_not_restrict_shared_write(app, rpc, rpc_token, tool):
    ctx = app.state.ctx
    run, token = rpc_token
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        lane = await lanes.upsert(
            conn, source="t3", external_id="team", title="Acme work", owner_person_id=ada["id"]
        )
        ask = await asks.create(
            conn, owner_person_id=ada["id"], lane_id=lane["id"], question="Continue?"
        )
        await rules.create(conn, owner_person_id=ada["id"], scope="team", text="Team rule")
    await rpc(
        token,
        "tools/call",
        {"name": tool, "arguments": {"id": ask["id"]} if tool == "asks_get" else {}},
    )
    async with ctx.pool.connection() as conn:
        assert not (await one(conn, "SELECT * FROM runs WHERE id=%s", (run["id"],)))[
            "personal_read"
        ]
