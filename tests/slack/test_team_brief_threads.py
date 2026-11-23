"""Team brief replies need no mention for 24 hours, with the usual identity checks."""

from datetime import datetime, timedelta

import pytest

from illo.channels.slack.intake import SlackIntake
from illo.db import one
from illo.ledger import kv
from illo.routines.team_digest import send_team_due
from tests.asks.test_digest import NOW
from tests.asks.test_team_digest import configure, seed_team


async def prepare(app, slack_client, monkeypatch):
    ctx = app.state.ctx
    configure(ctx)
    await seed_team(ctx)
    due = NOW + timedelta(hours=5)
    await send_team_due(ctx, due)
    root = next(iter(slack_client.messages))
    clock = {"now": due + timedelta(hours=23)}

    class Clock:
        fromisoformat = staticmethod(datetime.fromisoformat)
        fromtimestamp = staticmethod(datetime.fromtimestamp)

        @staticmethod
        def now(tz):
            return clock["now"]

    monkeypatch.setattr("illo.channels.slack.intake.datetime", Clock)
    intake = SlackIntake(ctx, "U_ACME_ILLO")
    event = {
        "type": "message",
        "channel": "C_ACME_TEAM",
        "ts": "100.1",
        "thread_ts": root,
        "user": "U_ACME_ADA",
        "text": "Please coordinate the review.",
    }
    return ctx, intake, event, clock, due


async def test_team_reply_has_context_and_expires_after_24_hours(
    app, fake_brain, slack_client, monkeypatch
):
    ctx, intake, event, clock, due = await prepare(app, slack_client, monkeypatch)
    result = await intake.receive({"event": event})
    assert result["decision"] == "run"
    async with ctx.pool.connection() as conn:
        run = await one(conn, "SELECT * FROM runs ORDER BY id DESC LIMIT 1")
        assert not await kv.get(conn, f"slack:thread:C_ACME_TEAM:{event['thread_ts']}")
    assert run["profile"] == "team_channel" and run["principal"] == "team" and not run["tainted"]
    assert '<outside_data source="thread">' in fake_brain.requests[-1].user_message
    assert "The lane is working." in fake_brain.requests[-1].user_message
    count = len(fake_brain.requests)
    clock["now"] = due + timedelta(hours=24)
    result = await intake.receive({"event": {**event, "ts": "101.1"}})
    assert result["decision"] == "ignore" and len(fake_brain.requests) == count
    result = await intake.receive(
        {"event": {**event, "ts": "102.1", "text": "<@U_ACME_ILLO> Please coordinate the review."}}
    )
    assert result["decision"] == "run"


@pytest.mark.parametrize("change", ["unknown", "outside", "off", "private", "bot"])
async def test_team_thread_keeps_identity_privacy_and_taint_checks(
    app, slack_client, monkeypatch, change
):
    ctx, intake, event, _, _ = await prepare(app, slack_client, monkeypatch)
    if change == "unknown":
        event["user"] = "U_UNKNOWN"
    elif change == "outside":
        event["user_team"] = "T_OUTSIDE"
    elif change == "off":
        ctx.workspace.config.slack.channels[0].mode = "off"
    elif change == "private":
        ctx.workspace.config.slack.channels[0].visibility = "private"
    else:
        event.update(subtype="bot_message", bot_id="B_OUTSIDE")
    result = await intake.receive({"event": event})
    if change == "private":
        assert result["decision"] == "run"
    elif change == "bot":
        assert result["decision"] == "run"
        async with ctx.pool.connection() as conn:
            run = await one(conn, "SELECT * FROM runs ORDER BY id DESC LIMIT 1")
        assert run["tainted"] and run["profile"] == "tainted"
    else:
        assert result is None or result["decision"] == "ignore"
