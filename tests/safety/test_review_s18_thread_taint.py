"""Rule 6: outside authors in Slack context narrow the run grant."""

import pytest

from illo.channels.slack.intake import SlackIntake
from illo.db import one


@pytest.mark.parametrize("author", [
    {"user": "U_OUTSIDE"},
    {"user": "U_ACME_BEN", "bot_id": "B_FORM"},
    {"user": "U_ACME_BEN", "subtype": "bot_message"},
])
@pytest.mark.parametrize("in_preparer", [False, True])
async def test_rule_06_outside_thread_context_taints_before_tools(
    app, slack_client, fake_brain, author, in_preparer
):
    ctx = app.state.ctx
    intake = SlackIntake(ctx, "U_ACME_ILLO")
    slack_client.messages = {"100.1": {"ts": "100.1", "text": "Start assigned work", **author}}
    if in_preparer:
        intake.adapter.prepare = None
        async def prepare(ctx, event, authority):
            await intake.prepare_context(event)
        ctx.gateway.register_preparer(prepare)
    fake_brain.script = [{"name": "github_assign_issue", "arguments": {
        "repo": "acme-example/engine", "number": 1, "assignee": "ben",
    }}]
    result = await intake.receive({"event": {
        "type": "message", "channel": "C_ACME_ALERTS", "ts": "101.1",
        "thread_ts": "100.1", "user": "U_ACME_ADA", "text": "Handle this",
    }})
    assert "error" not in result
    async with ctx.pool.connection() as conn:
        run = await one(conn, "SELECT * FROM runs")
        event = await one(conn, "SELECT * FROM events")
    assert run["tainted"] and run["profile"] == "tainted" and event["tainted"]
    assert fake_brain.results[0]["structuredContent"]["decision"] == "deny"
