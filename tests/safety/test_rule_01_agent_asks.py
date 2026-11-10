"""Rule 1: a brain-created ask keeps the authenticated agent requester."""

from illo.asks import relay
from illo.db import one
from illo.ledger import asks, people
from tests.mcp.client import AgentClient


async def test_rule_01_agent_brain_ask_returns_through_pull(app, http, fake_brain, slack_client):
    ctx = app.state.ctx
    await ctx.scheduler.close()
    agent = await AgentClient.create(app, http)
    fake_brain.script = [{"name": "asks_create", "arguments": {
        "owner": "ben", "question": "Can the new runner take the GPU jobs?"
    }}]
    await agent.call("illo_submit", {"message": "Ask Ben if the new runner can take the GPU jobs."})
    await agent.settle()
    async with ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM asks")
        assert row["requester_token_id"] == agent.token_id
        assert row["requester_person_id"] == agent.person_id
        ben = await people.get(conn, "ben")
        await asks.answer(conn, row["id"], person_id=ben["id"], text="Yes")
    await relay.tick(ctx)
    assert not slack_client.calls
    answers = (await agent.call("ask_pull"))["answers"]
    assert len(answers) == 1
    assert answers[0]["ask_id"] == row["id"]
    assert answers[0]["answer_text"] == "Yes"
    await agent.call("ask_ack", {"ask_id": row["id"]})
    assert (await agent.call("ask_pull"))["answers"] == []
