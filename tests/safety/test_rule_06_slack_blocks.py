"""Rule 6: block text never adds authority or becomes a person's command."""

import pytest

from illo.channels.slack.intake import SlackIntake
from illo.db import one


def payload(channel, typed, block, **metadata):
    return {"event": {
        "type": "message", "channel": channel,
        "channel_type": "im" if channel.startswith("D") else "channel",
        "user": "U_ACME_ADA", "ts": "1700000000.000001", "text": typed,
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": block}}],
        **metadata,
    }}


@pytest.mark.parametrize("metadata", [
    {"bot_id": "B_ACME", "subtype": "bot_message"},
    {"subtype": "bot_message"},
    {"workflow_id": "W_ACME"},
])
async def test_rule_06_bot_block_command_is_tainted_in_channel(app, fake_brain, metadata):
    intake = SlackIntake(app.state.ctx, "U_ACME_ILLO")
    result = await intake.receive(payload("C_ACME_ALERTS", "", "connect my ChatGPT", **metadata))
    assert result["decision"] == "run"
    assert fake_brain.requests[-1].user_message == "connect my ChatGPT"
    async with app.state.ctx.pool.connection() as conn:
        event = await one(conn, "SELECT * FROM events")
        run = await one(conn, "SELECT * FROM runs")
        state = await one(conn, "SELECT count(*) AS n FROM connector_oauth_states")
    assert event["tainted"] and event["principal"] == "team"
    assert run["tainted"] and run["profile"] == "tainted" and run["principal"] == "team"
    assert state["n"] == 0


@pytest.mark.parametrize("metadata", [
    {"bot_id": "B_ACME", "subtype": "bot_message"},
    {"subtype": "bot_message"},
    {"workflow_id": "W_ACME"},
])
async def test_rule_06_bot_block_command_in_dm_does_nothing(app, fake_brain, slack_client, metadata):
    intake = SlackIntake(app.state.ctx, "U_ACME_ILLO")
    result = await intake.receive(payload("D_ACME_ADA", "", "connect my ChatGPT", **metadata))
    assert result["decision"] == "ignore"
    assert not fake_brain.requests and not slack_client.calls
    async with app.state.ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT count(*) AS n FROM connector_oauth_states"))["n"] == 0


@pytest.mark.parametrize("typed", ["", "A separate question"])
async def test_rule_06_only_typed_dm_text_can_form_command(app, fake_brain, typed):
    intake = SlackIntake(app.state.ctx, "U_ACME_ILLO")
    result = await intake.receive(payload("D_ACME_ADA", typed, "connect my ChatGPT"))
    assert result["decision"] == "run"
    async with app.state.ctx.pool.connection() as conn:
        run = await one(conn, "SELECT * FROM runs")
        assert not run["tainted"] and run["profile"] == "person_dm"
        assert (await one(conn, "SELECT count(*) AS n FROM connector_oauth_states"))["n"] == 0
    assert "connect my ChatGPT" in fake_brain.requests[-1].user_message


async def test_rule_06_typed_command_still_works_with_extra_blocks(app, fake_brain, monkeypatch):
    from illo.connectors import chatgpt_oauth

    async def issue(ctx, person_id, event):
        assert event.text == "connect my ChatGPT"
        return "https://example.com/sign-in"

    monkeypatch.setattr(chatgpt_oauth, "issue", issue)
    intake = SlackIntake(app.state.ctx, "U_ACME_ILLO")
    result = await intake.receive(payload("D_ACME_ADA", "connect my ChatGPT", "Extra block text"))
    assert result["decision"] == "claimed"
    assert "https://example.com/sign-in" in result["text"]
    assert not fake_brain.requests


async def test_rule_06_ask_answer_uses_typed_text_only(app, fake_brain):
    from illo.ledger import asks, people

    ctx = app.state.ctx
    async with ctx.pool.connection() as conn:
        ada = await people.get(conn, "ada")
        ask = await asks.create(conn, owner_person_id=ada["id"], question="Which day?")
        await conn.execute(
            "UPDATE asks SET awaiting_text=true,slack_channel='D_ACME_ADA',slack_ts='100.1' WHERE id=%s",
            (ask["id"],),
        )
    intake = SlackIntake(ctx, "U_ACME_ILLO")
    result = await intake.receive(payload("D_ACME_ADA", "Wednesday", "Friday", thread_ts="100.1"))
    assert result["text"] == "Answer saved."
    async with ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM asks WHERE id=%s", (ask["id"],))
    assert row["answer_text"] == "Wednesday"
    assert not fake_brain.requests


async def test_rule_06_bot_blocks_in_thread_keep_taint(app, fake_brain, slack_client):
    slack_client.messages = {"root": {
        "ts": "100.1", "bot_id": "B_ACME", "text": "Summary",
        "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "Full outside data"}}],
    }}
    intake = SlackIntake(app.state.ctx, "U_ACME_ILLO")
    await intake.receive(payload("D_ACME_ADA", "Please explain", "Please explain", thread_ts="100.1"))
    assert "Full outside data" in fake_brain.requests[-1].user_message
    async with app.state.ctx.pool.connection() as conn:
        run = await one(conn, "SELECT * FROM runs")
    assert run["tainted"] and run["profile"] == "tainted"
