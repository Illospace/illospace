"""A sign-in must name the person or an explicitly shared team account."""

from urllib.parse import urlencode

import pytest

from illo.connectors import chatgpt, chatgpt_oauth
from illo.ledger import chatgpt as grants
from tests.chatgpt.conftest import jwt, tokens
from tests.chatgpt.test_signin import issue_start


@pytest.mark.parametrize("device", [False, True])
@pytest.mark.parametrize(
    "email,shared,allowed",
    [
        ("ben@other.example", False, False),
        (None, False, False),
        ("ADA@EXAMPLE.COM", False, True),
        ("shared@example.com", True, True),
    ],
)
async def test_account_binding(
    chatgpt_app, replay, monkeypatch, slack_client, device, email, shared, allowed
):
    app, backend = chatgpt_app
    ctx = app.state.ctx
    backend.device = device
    backend.pending = False
    if shared:
        ctx.workspace.config.brain.chatgpt_allowed_accounts = [email]
    state, person, _, _ = await issue_start(ctx, paste=not device)

    async def exchange(*args):
        result = tokens()
        result["id_token"] = jwt(
            email=email, **{"https://api.openai.com/auth": {"chatgpt_account_id": "other-account"}}
        )
        return result

    monkeypatch.setattr(ctx.chatgpt_client, "exchange", exchange)
    if device:
        await chatgpt_oauth.poll_devices(ctx)
        text = slack_client.calls[-1]["text"]
    else:
        url = chatgpt.REDIRECT_URI + "?" + urlencode({"code": "fake", "state": state})
        text = (await replay(text=url))["text"]
    async with ctx.pool.connection() as conn:
        assert bool(await grants.load(conn, person["id"])) == allowed
    if allowed:
        assert (
            text == f"ChatGPT is connected as {email}. I use your subscription for your requests."
        )
    elif email:
        assert "not one of your addresses in people.yaml" in text
        assert email not in text
    else:
        assert text == chatgpt_oauth.FAILED
