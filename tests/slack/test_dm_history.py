"""DM history adds bounded context without changing the stored request."""

import pytest

from illo.channels.slack.threads import read_dm_history
from illo.db import one
from illo.gateway import InboundEvent


def event():
    return InboundEvent(channel="slack", external_id="dm-history", kind="dm",
                        sender="U_ACME_ADA", text="What is it?",
                        payload={"channel_id": "D_ACME_ADA", "message_ts": "1700000100"})


async def test_order_names_cards_and_local_time(app, slack_client):
    slack_client.messages = {
        "2": {"ts": "1700000002", "bot_id": "B_ILLO", "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": "Ticket card"}}]},
        "1": {"ts": "1700000001", "user": "U_ACME_ADA", "text": "Hello"},
    }
    request = event()
    lines = await read_dm_history(app.state.ctx, request, "U_ACME_ILLO", None,
                                  bot_identity="B_ILLO")
    assert lines == ["[2023-11-14 23:13] Ada: Hello", "[2023-11-14 23:13] Illo: Ticket card"]
    assert not request.payload.get("contains_outside_text")
    call = slack_client.calls[0]
    assert call == {"method": "conversations_history", "channel": "D_ACME_ADA",
                    "latest": "1700000100", "inclusive": False, "limit": 30}


async def test_caps_subtypes_and_taint(app, slack_client):
    slack_client.messages = {str(i): {"ts": str(i), "user": "U_FOREIGN", "text": str(i)}
                             for i in range(30, 0, -1)}
    slack_client.messages["working"] = {"ts": "working", "text": "Looking…"}
    slack_client.messages["joined"] = {"ts": "31", "subtype": "channel_join", "text": "Joined"}
    request = event()
    lines = await read_dm_history(app.state.ctx, request, "U_ACME_ILLO", "working")
    assert len(lines) == 20 and lines[0].endswith("Automation: 11")
    assert lines[-1].endswith("Automation: 30")
    assert request.payload["contains_outside_text"]
    for m in slack_client.messages.values():
        m["text"] = "x" * 2000
    lines = await read_dm_history(app.state.ctx, event(), "U_ACME_ILLO", "working")
    assert len("\n".join(lines)) <= 6000
    assert all(line.endswith(" …") for line in lines)
    assert all(len(line.split(": ", 1)[1]) == 1500 for line in lines)


@pytest.mark.parametrize("failure", [RuntimeError, TimeoutError])
async def test_failure_continues_and_sets_sensor(intake, message, slack_client, fake_brain, app, failure):
    async def fail(**kwargs):
        raise failure("history unavailable")
    slack_client.conversations_history = fail
    result = await intake.receive(message())
    assert result["decision"] == "run" and "error" not in result
    assert fake_brain.requests[-1].user_message == "Give me a lane update"
    async with app.state.ctx.pool.connection() as conn:
        sensor = await one(conn, "SELECT * FROM sensors WHERE key='slack:dm-history'")
    assert sensor["last_error_at"]
    assert sensor["detail"] == "Slack could not read the DM history. Check the `im:history` scope."


async def test_context_preserves_stored_text_and_claims(intake, message, slack_client, app, fake_brain):
    slack_client.messages = {"old": {"ts": "1699999999", "user": "U_ACME_ILLO", "text": "Ticket 301"}}
    seen = []
    async def claim(ctx, request, authority):
        seen.append(request.text)
    app.state.ctx.gateway.register_claim(claim)
    result = await intake.receive(message(text="What is it?"))
    assert result["decision"] == "run"
    assert seen == ["What is it?"]
    assert '<outside_data source="dm-history">' in fake_brain.requests[-1].user_message
    async with app.state.ctx.pool.connection() as conn:
        stored = await one(conn, "SELECT * FROM events WHERE id=%s", (result["event_id"],))
        assert stored["payload"]["text"] == "What is it?"
        assert not await one(conn, "SELECT * FROM items WHERE kind='slack'")
        assert (await one(conn, "SELECT * FROM sensors WHERE key='slack:dm-history'"))["last_ok_at"]


async def test_thread_still_uses_replies(intake, message, slack_client, fake_brain):
    await intake.receive(message(thread_ts="1699999999"))
    assert any(c["method"] == "conversations_replies" for c in slack_client.calls)
    assert not any(c["method"] == "conversations_history" for c in slack_client.calls)


async def test_final_message_budget(intake, app, slack_client):
    request = event()
    slack_client.messages = {"old": {"ts": "1", "user": "U_ACME_ADA", "text": "a" * 2000}}
    app.state.ctx.workspace.config.run_limits.max_message_chars = 150
    await intake.prepare_context(request)
    assert len(request.text) <= 150
    assert request.text.endswith("Request:\nWhat is it?")
