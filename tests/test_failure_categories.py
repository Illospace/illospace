"""Failure replies use fixed words. Logs never contain exception text."""

import pytest

from illo.brain.client import BrainEvent
from illo.channels.slack.adapter import SlackAdapter
from illo.db import one
from illo.gateway import InboundEvent

TEXTS = {
    "limit": "I could not finish this: I reached my step limit. Ask again, or ask for less.",
    "timeout": "I could not finish this in time. Ask again, or ask for less.",
    "provider": "I could not finish this: the model service failed. Ask again in a minute.",
    "internal": "I could not finish this because of an internal error. Ask again in a minute.",
}


@pytest.mark.parametrize("category", TEXTS)
@pytest.mark.parametrize("slack", [False, True])
async def test_failure_has_a_fixed_category_without_secret_text(
    app, channel, caplog, category, slack
):
    ctx = app.state.ctx

    class FailedBrain:
        async def run(self, request):
            if category == "internal":
                raise ValueError("fake-credential-in-an-error")
            yield BrainEvent(type="error", category={
                "provider": "runtime", "limit": "limit", "timeout": "timeout",
            }[category], message="fake-credential-in-an-error")

    ctx.brain_client = FailedBrain()
    event = InboundEvent(channel="slack", external_id="failure", kind="dm", sender="U_ACME_ADA",
                         text="Check the release.", payload={"channel_id": "D_ADA"})
    result = await ctx.gateway.receive(event, SlackAdapter(ctx) if slack else channel)
    assert result["category"] == category and result["error"] == TEXTS[category]
    if slack:
        assert ctx.slack_client.calls[-1]["text"] == TEXTS[category]
    else:
        assert channel.errors[-1][1] == TEXTS[category]
    assert "category=" + category in caplog.text
    assert "exception_type=" + ("ValueError" if category == "internal" else "RunFailure") in caplog.text
    assert "fake-credential-in-an-error" not in caplog.text + str(result)
    async with ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM runs")
    assert row["status"] == ("timeout" if category == "timeout" else "failed")
    assert row["token_hash"] is None and "fake-credential" not in row["error"]


@pytest.mark.parametrize("category", TEXTS)
async def test_agent_receipt_keeps_the_failure_category(app, http, category):
    from tests.mcp.client import AgentClient

    class FailedBrain:
        async def run(self, request):
            if category == "internal":
                raise ValueError("fake-credential-in-an-error")
            yield BrainEvent(type="error", category={
                "provider": "auth", "limit": "limit", "timeout": "timeout",
            }[category])

    app.state.ctx.brain_client = FailedBrain()
    agent = await AgentClient.create(app, http)
    accepted = await agent.call("illo_submit", {"message": "Check the release."})
    await agent.settle()
    result = await agent.call("illo_get_result", {"event_id": accepted["event_id"]})
    assert result["failure"] == {"category": category, "message": TEXTS[category]}
    assert result["final_answer"] == TEXTS[category]
    # A receipt can be reconciled after delivery failed. The run keeps the category.
    async with app.state.ctx.pool.connection() as conn:
        await conn.execute("UPDATE submissions SET result=result-'failure'")
    result = await agent.call("illo_get_result", {"event_id": accepted["event_id"]})
    assert result["failure"] == {"category": category, "message": TEXTS[category]}


@pytest.mark.parametrize("error,category", [(TimeoutError, "timeout"), (ConnectionError, "internal")])
async def test_local_exception_category_uses_only_its_type(app, replay, caplog, error, category):
    class FailedBrain:
        async def run(self, request):
            raise error("fake-credential-in-an-error")
            yield

    app.state.ctx.brain_client = FailedBrain()
    result = await replay()
    assert result["category"] == category and result["error"] == TEXTS[category]
    assert "fake-credential" not in caplog.text + str(result)
