import asyncio
from dataclasses import replace

import pytest

from illo.db import one
from illo.tools.registry import Arguments
from tests.mcp.client import AgentClient
from tests.mcp.test_asks import ASK


async def test_rule_01_open_ask_cap_is_atomic_per_owner_and_token(app, http):
    agent = await AgentClient.create(app, http)
    args = {**ASK, "owner": "ben", "lane": None}
    for i in range(19):
        await agent.call("ask_create", {**args, "source_ref": str(i)})
    responses = await asyncio.gather(
        *(agent.tool_response("ask_create", {**args, "source_ref": str(i)}) for i in range(19, 22))
    )
    assert sum(not r["result"]["isError"] for r in responses) == 1
    assert (await agent.call("ask_create", {**args, "source_ref": "0"}))["state"] == "open"
    await agent.call("ask_cancel", {"source_ref": "0"})
    await agent.call("ask_create", {**args, "source_ref": "replacement"})
    await agent.call("ask_create", {**args, "owner": "ada", "source_ref": "other-owner"})
    other = await AgentClient.create(app, http)
    await other.call("ask_create", {**args, "source_ref": "other-token"})


@pytest.mark.parametrize("name", ["signal_submit", "illo_submit"])
async def test_rule_01_hourly_caps_keep_retries_saves_and_other_tokens(app, http, name):
    agent = await AgentClient.create(app, http)

    def args(i):
        if name == "signal_submit":
            return {
                "source": "acme-monitor",
                "dedupe_key": str(i),
                "severity": "warning",
                "title": "Check failed",
                "body": "The check needs review.",
            }
        return {"message": "Give me a lane update.", "idempotency_key": str(i)}

    for i in range(59):
        await agent.call(name, args(i))
    responses = await asyncio.gather(*(agent.tool_response(name, args(i)) for i in range(59, 62)))
    assert sum(not r["result"]["isError"] for r in responses) == 1
    await agent.call(name, args(0))
    other = await AgentClient.create(app, http)
    await other.call(name, args("other-token"))
    if name == "illo_submit":
        saved = await agent.call(
            name, {"message": "Acme uses the preview.", "desired_outcome": "preserve_knowledge"}
        )
        assert saved["evidence_status"] == "satisfied"
    async with app.state.ctx.pool.connection() as conn:
        table = "signals" if name == "signal_submit" else "submissions"
        await conn.execute(f"UPDATE {table} SET created_at=now()-interval '61 minutes'")
    await agent.call(name, args("next-hour"))
    await agent.settle()
    await other.settle()


@pytest.mark.parametrize(
    "name,limit",
    [
        ("asks_create", 3),
        ("slack_send_dm", 3),
        ("github_create_issue", 3),
        ("github_comment_issue", 5),
        ("work_start", 1),
    ],
)
async def test_rule_01_run_caps_count_audit_before_side_effect(app, rpc_token, rpc, name, limit):
    ctx = app.state.ctx
    called = []

    async def handler(ctx, args):
        called.append(name)
        return {"sent": True}

    ctx.registry.tools[name] = replace(
        ctx.registry.tools[name],
        handler=handler,
        arguments=Arguments,
        preflight=None,
        destination=lambda _: "ledger",
    )
    run, raw = rpc_token
    expected = "draft" if name == "github_comment_issue" else "allow"
    for _ in range(limit):
        response = await rpc(raw, "tools/call", {"name": name, "arguments": {}})
        assert response.json()["result"]["structuredContent"]["decision"] == expected
    response = await rpc(raw, "tools/call", {"name": name, "arguments": {}})
    result = response.json()["result"]["structuredContent"]
    assert result["decision"] == "deny"
    assert "limit" in result["reason"]
    assert len(called) == (0 if expected == "draft" else limit)
    async with ctx.pool.connection() as conn:
        assert (
            await one(
                conn,
                "SELECT count(*) AS n FROM audit WHERE run_id=%s AND tool=%s",
                (run["id"], name),
            )
        )["n"] == limit + 1
