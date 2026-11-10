"""Rule 6: only Illo answers from tainted runs taint a person's follow-up."""

import pytest

from illo.channels.slack.intake import SlackIntake
from illo.db import one
from illo.ledger import kv


@pytest.mark.parametrize("where", ["channel", "dm"])
@pytest.mark.parametrize("tainted", [False, True])
async def test_rule_06_person_followup_keeps_profile_unless_thread_is_tainted(
    app, slack_client, fake_brain, where, tainted
):
    ctx = app.state.ctx
    intake = SlackIntake(ctx, "U_ACME_ILLO")
    channel = "C_ACME_TEAM" if where == "channel" else "D_ACME_ADA"
    slack_client.messages = {
        "100.1": {"ts": "100.1", "user": "U_ACME_ADA", "text": "Lane update?"},
        "100.2": {"ts": "100.2", "user": "U_ACME_ILLO", "bot_id": "B_ILLO",
                  "subtype": "bot_message", "text": "Ben owns two lanes."},
    }
    async with ctx.pool.connection() as conn:
        await kv.set(conn, f"slack:thread:{channel}:100.1", True)
        if tainted:
            await kv.set(conn, f"slack:thread-tainted:{channel}:100.1", True)
    fake_brain.script = [{"name": "lanes_search", "arguments": {"query": "Ben"}}]
    result = await intake.receive({"event": {
        "type": "message", "channel": channel, "ts": "101.1", "thread_ts": "100.1",
        "user": "U_ACME_ADA", "text": "And what blocks Ben?",
    }})
    assert "error" not in result
    async with ctx.pool.connection() as conn:
        run = await one(conn, "SELECT * FROM runs ORDER BY id DESC LIMIT 1")
    assert run["tainted"] is tainted
    assert run["profile"] == ("tainted" if tainted else "person_dm" if where == "dm" else "team_channel")
    assert fake_brain.results[0]["structuredContent"]["decision"] == ("deny" if tainted else "allow")


async def test_rule_06_tainted_answer_sets_durable_marker(app, slack_client):
    ctx = app.state.ctx
    intake = SlackIntake(ctx, "U_ACME_ILLO")
    result = await intake.receive({"event": {
        "type": "message", "subtype": "bot_message", "bot_id": "B_HOOK",
        "channel": "C_ACME_ALERTS", "ts": "200.1", "text": "Machine alert",
    }})
    assert result["decision"] == "run"
    async with ctx.pool.connection() as conn:
        assert await kv.get(conn, "slack:thread-tainted:C_ACME_ALERTS:200.1") is True
    # The original outside post may no longer be in the retained thread context.
    slack_client.messages = {"200.2": {"ts": "200.2", "user": "U_ACME_ILLO", "text": "I filed a ticket."}}
    result = await SlackIntake(ctx, "U_ACME_ILLO").receive({"event": {
        "type": "message", "channel": "C_ACME_ALERTS", "ts": "201.1", "thread_ts": "200.1",
        "user": "U_ACME_ADA", "text": "Tell me more",
    }})
    assert result["decision"] == "run"
    async with ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT tainted FROM runs ORDER BY id DESC LIMIT 1"))["tainted"]
