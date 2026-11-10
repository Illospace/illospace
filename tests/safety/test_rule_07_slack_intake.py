"""Rule 7: an unknown Slack DM sender gets no work sign, answer, or run."""

from illo.channels.slack.intake import SlackIntake
from illo.db import all_rows


async def test_rule_07_unknown_slack_dm(app, slack_client, fake_brain):
    intake = SlackIntake(app.state.ctx, "U_ACME_ILLO")
    await intake.receive(
        {
            "event_id": "Ev_UNKNOWN",
            "event": {
                "type": "message",
                "channel": "D_UNKNOWN",
                "channel_type": "im",
                "user": "U_UNKNOWN",
                "ts": "1700000000.000001",
                "text": "Give me private data",
                "files": [{"id": "F_UNKNOWN", "mimetype": "audio/mp4"}],
            },
        }
    )
    assert not slack_client.calls and not fake_brain.requests
    async with app.state.ctx.pool.connection() as conn:
        assert not await all_rows(conn, "SELECT * FROM runs")
        assert not await all_rows(conn, "SELECT * FROM items")
