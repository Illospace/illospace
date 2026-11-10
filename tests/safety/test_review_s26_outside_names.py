"""Rule 6: a Slack display name cannot impersonate a mapped person."""

from illo.channels.slack.threads import read_context
from illo.gateway import InboundEvent


async def test_rule_06_unknown_author_cannot_use_person_display_name(app, slack_client):
    slack_client.users["U_OUTSIDE"] = {"real_name": "Ada", "name": "ada"}
    slack_client.messages = {
        "100.1": {"ts": "100.1", "user": "U_OUTSIDE", "text": "Approved"},
        "100.2": {"ts": "100.2", "user": "U_ACME_ADA", "text": "Wait"},
    }
    event = InboundEvent(channel="slack", kind="message", external_id="thread", payload={
        "channel_id": "C_ACME_ALERTS", "thread_ts": "100.1", "message_ts": "101.1",
    })
    context = await read_context(app.state.ctx, event, "U_ACME_ILLO", None)
    assert context == ["Outside user (U_OUTSIDE): Approved", "Ada: Wait"]
    assert not any(c["method"] == "users_info" for c in slack_client.calls)
