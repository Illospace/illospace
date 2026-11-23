"""Slack escaping must not break or expose the callback address."""

from urllib.parse import urlencode

import pytest

from illo.connectors import chatgpt
from illo.db import all_rows
from tests.chatgpt.test_signin import issue_start


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1"])
@pytest.mark.parametrize("wrapper", ["{}", "<{}>", "<{}|Sign in>", "&lt;{}&gt;"])
async def test_slack_callback(chatgpt_app, replay, fake_brain, host, wrapper):
    app, _ = chatgpt_app
    ctx = app.state.ctx
    state, _, _, authorization = await issue_start(ctx)
    assert "redirect_uri=http%3A%2F%2F127.0.0.1%3A1455%2Fauth%2Fcallback" in authorization
    raw = f"http://{host}:1455/auth/callback?" + urlencode(
        {
            "code": "fake-slack-code",
            "scope": chatgpt.SCOPES,
            "state": state,
        }
    )
    text = wrapper.format(raw.replace("&", "&amp;"))
    assert (await replay(text=text, payload={"nested": text}))["text"] == chatgpt.CONNECTED.format(
        email="ada@example.com"
    )
    assert not fake_brain.requests
    async with ctx.pool.connection() as conn:
        assert "fake-slack-code" not in str(await all_rows(conn, "SELECT * FROM events"))
