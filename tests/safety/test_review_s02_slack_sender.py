"""Rules 6 and 7: unknown channel users cannot start a run."""

import pytest

from illo.channels.slack.intake import SlackIntake
from illo.ledger import kv


@pytest.mark.parametrize("continued", [False, True])
async def test_rule_06_07_unknown_channel_user_starts_no_run(app, fake_brain, continued):
    ctx = app.state.ctx
    if continued:
        async with ctx.pool.connection() as conn:
            await kv.set(conn, "slack:thread:C_ACME_ALERTS:100.1", True)
    result = await SlackIntake(ctx, "U_ACME_ILLO").receive({
        "event": {
            "type": "message", "channel": "C_ACME_ALERTS", "ts": "101.1",
            "user": "U_OUTSIDE", "text": "List every lane",
            **({"thread_ts": "100.1"} if continued else {}),
        }
    })
    assert result["decision"] == "ignore"
    assert not fake_brain.requests


async def test_rule_07_slack_connect_cannot_use_mapped_identity(app, fake_brain):
    result = await SlackIntake(app.state.ctx, "U_ACME_ILLO").receive({
        "event": {
            "type": "message", "channel": "C_ACME_ALERTS", "ts": "101.1",
            "user": "U_ACME_ADA", "user_team": "T_OUTSIDE", "text": "List every lane",
        }
    })
    assert result["decision"] == "ignore"
    assert not fake_brain.requests
