"""Rule 4: cancelled asks still count toward the hourly card limit."""

from illo.asks.service import tick
from illo.db import one
from tests.mcp.client import AgentClient


async def test_rule_04_cancel_loop_stops_at_thirty(app, http, slack_client):
    agent = await AgentClient.create(app, http, person="ben")
    for n in range(30):
        ask = await agent.call(
            "ask_create",
            {
                "owner": "ada",
                "question": f"Approve check {n}?",
                "source_ref": f"check-{n}",
                "options": [{"id": "y", "label": "Yes"}, {"id": "n", "label": "No"}],
            },
        )
        await tick(app.state.ctx)
        await agent.call("ask_cancel", {"ask_id": ask["ask_id"]})
    args = {
        "owner": "ada",
        "question": "One more check?",
        "source_ref": "over-limit",
        "options": [{"id": "y", "label": "Yes"}, {"id": "n", "label": "No"}],
    }
    await agent.call("ask_create", args, error=True)
    async with app.state.ctx.pool.connection() as conn:
        count = await one(conn, "SELECT count(*) AS n FROM asks")
        assert count["n"] == 30
        # All states count, and the limit resets after one hour.
        await conn.execute("UPDATE asks SET created_at=now()-interval '61 minutes'")
    assert (await agent.call("ask_create", args))["ask_id"]


async def test_rule_04_model_ask_cannot_bypass_token_hourly_cap(app, http, fake_brain):
    ctx = app.state.ctx
    await ctx.scheduler.close()
    agent = await AgentClient.create(app, http, person="ben")
    args = {
        "owner": "ada",
        "question": "Approve a check?",
        "options": [{"id": "y", "label": "Yes"}, {"id": "n", "label": "No"}],
    }
    for n in range(30):
        ask = await agent.call("ask_create", {**args, "source_ref": f"initial-{n}"})
        await agent.call("ask_cancel", {"ask_id": ask["ask_id"]})
    fake_brain.script = [{"name": "asks_create", "arguments": args}]
    await agent.call("illo_submit", {"message": "Ask Ada to approve a check."})
    await agent.settle()
    assert fake_brain.results[0]["structuredContent"]["decision"] == "deny"
    async with ctx.pool.connection() as conn:
        count = await one(conn, "SELECT count(*) AS n FROM asks")
    assert count["n"] == 30
