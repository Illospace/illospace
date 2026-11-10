"""Rule 6: Slack bot, webhook, and workflow metadata narrows the run grant."""

import pytest

from illo.channels.slack.intake import SlackIntake
from illo.db import one


@pytest.mark.parametrize(
    "metadata",
    [
        {"bot_id": "B_ACME", "subtype": "bot_message"},
        {"subtype": "bot_message"},
        {"workflow_id": "W_ACME"},
    ],
)
async def test_rule_06_slack_automation(app, fake_brain, metadata):
    intake = SlackIntake(app.state.ctx, "U_ACME_ILLO")
    await intake.receive(
        {
            "event_id": "Ev_BOT",
            "event": {
                "type": "message",
                "channel": "C_ACME_ALERTS",
                "channel_type": "channel",
                "ts": "1700000000.000001",
                "text": "The service failed",
                **metadata,
            },
        }
    )
    async with app.state.ctx.pool.connection() as conn:
        run = await one(conn, "SELECT * FROM runs")
        assert run["tainted"] and run["principal"] == "team" and run["profile"] == "tainted"
    assert len(fake_brain.requests) == 1
