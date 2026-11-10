"""A sign-in claim takes only one matching callback URL in its owner's DM."""

import pytest

from illo.connectors import oauth
from illo.gateway import InboundEvent
from illo.gateway.principal import Authority


@pytest.mark.parametrize(
    "text",
    [
        "Explain /oauth/google/callback to me.",
        "Debug http://localhost:3000/login?code=example",
        "http://127.0.0.1:9000/login?state=example",
        "https://other.example/oauth/google/callback?state=example&code=example",
        "http://test/oauth/google/callback-other?code=example",
        "Explain http://test/oauth/google/callback?code=example&state=example",
        "http://test/oauth/google/callback?code=example&state=example https://example.com/",
    ],
)
async def test_unrelated_text_does_not_get_claimed(app, text):
    event = InboundEvent(
        channel="slack", external_id="test", kind="dm", sender="U_ACME_ADA", text=text
    )
    authority = Authority("person:ada", "person_dm", person_id=1)
    assert await oauth.claim(app.state.ctx, event, authority) is None


@pytest.mark.parametrize(
    "text",
    [
        "Explain /oauth/google/callback to me.",
        "Debug http://localhost:3000/login?code=example",
        "https://other.example/oauth/google/callback?state=example&code=example",
    ],
)
async def test_unrelated_text_stays_in_the_ledger_and_reaches_the_brain(
    app, replay, fake_brain, text
):
    result = await replay(text=text)
    assert result["decision"] == "run"
    assert text in fake_brain.requests[-1].user_message
    async with app.state.ctx.pool.connection() as conn:
        event = await (
            await conn.execute("SELECT payload FROM events WHERE id=%s", (result["event_id"],))
        ).fetchone()
    assert event["payload"]["text"] == text


@pytest.mark.parametrize(
    "channel,kind,authority",
    [
        ("slack", "message", Authority("team", "team_channel")),
        ("slack", "dm", Authority("person:ada", "person_dm", 1, True)),
        ("email", "message", Authority("person:ada", "person_dm", 1)),
    ],
)
async def test_callback_claim_does_not_take_a_channel_or_tainted_message(
    app, channel, kind, authority
):
    event = InboundEvent(
        channel=channel,
        external_id="test",
        kind=kind,
        text="http://test/oauth/google/callback?state=example&code=example",
    )
    assert await oauth.claim(app.state.ctx, event, authority) is None


@pytest.mark.parametrize(
    "text",
    [
        "Explain http://test/oauth/google/callback?code=example-code&state=example-state",
        "http://test/oauth/google/callback?code=example-code&state=example-state https://example.com/",
    ],
)
async def test_callback_inside_other_text_is_removed_before_the_brain(
    app, replay, fake_brain, text
):
    result = await replay(text=text)
    assert result["decision"] == "run"
    assert fake_brain.requests
    assert "example-code" not in fake_brain.requests[-1].user_message
    assert "example-state" not in fake_brain.requests[-1].user_message
    async with app.state.ctx.pool.connection() as conn:
        event = await (
            await conn.execute("SELECT payload FROM events WHERE id=%s", (result["event_id"],))
        ).fetchone()
    assert "example-code" not in str(event)
    assert "example-state" not in str(event)


@pytest.mark.parametrize("wrapper", ["{}", "<{}>"])
async def test_exact_callback_in_a_person_dm_still_gets_claimed(app, replay, fake_brain, wrapper):
    address = "http://test/oauth/google/callback?state=example&code=example"
    result = await replay(text=wrapper.format(address))
    assert result["decision"] == "claimed"
    assert not fake_brain.requests


async def test_channel_callback_continues_without_exposing_the_code(app, replay, fake_brain):
    channel = app.state.ctx.workspace.config.slack.channels[0]
    result = await replay(
        kind="message",
        text="http://test/oauth/google/callback?state=example-state&code=example-code",
        payload={"channel_id": channel.id, "mentioned": True},
    )
    assert result["decision"] == "run"
    assert "example-code" not in fake_brain.requests[-1].user_message
    assert "example-state" not in fake_brain.requests[-1].user_message
