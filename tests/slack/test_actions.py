"""The channel routes clicks. Each area checks action ownership."""

from types import SimpleNamespace

from illo.channels.slack.connection import SlackConnection


def click(user="U_ACME_ADA"):
    return {
        "type": "block_actions",
        "user": {"id": user},
        "channel": {"id": "D_ACME_ADA"},
        "message": {"ts": "100.001"},
        "trigger_id": "trigger",
        "actions": [{"action_id": "ask:answer:1", "value": "yes", "action_ts": "101.001"}],
    }


async def test_known_click_routes_once_unknown_does_nothing(app, intake):
    seen = []

    async def handler(ctx, person, action):
        seen.append((person, action))

    app.state.ctx.slack_actions.register("ask:", handler)
    await intake.receive(click())
    await intake.receive(click())
    await intake.receive(click("U_UNKNOWN"))
    assert len(seen) == 1
    assert seen[0][0]["key"] == "ada"
    assert seen[0][1] == {
        "action_id": "ask:answer:1",
        "value": "yes",
        "channel": "D_ACME_ADA",
        "message_ts": "100.001",
        "thread_ts": None,
        "trigger_id": "trigger",
        "user_id": "U_ACME_ADA",
    }


async def test_socket_ack_precedes_work(app, intake):
    seen = []
    connection = SlackConnection(app.state.ctx)

    class Client:
        async def send_socket_mode_response(self, response):
            seen.append(response.envelope_id)

    class Intake:
        async def receive(self, payload):
            assert seen == ["envelope"]
            seen.append("handled")

    connection.intake = Intake()
    await connection.on_request(
        Client(), SimpleNamespace(type="events_api", envelope_id="envelope", payload={"event": {}})
    )
    assert seen == ["envelope"]
    await __import__("asyncio").gather(*connection.pending)
    assert seen == ["envelope", "handled"]
