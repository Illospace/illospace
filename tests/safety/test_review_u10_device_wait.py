"""Device sign-in reports expiry and keeps temporary poll failures pending."""

import json

import httpx
import pytest

from illo.connectors import chatgpt, chatgpt_oauth, oauth
from illo.connectors.store import vault
from illo.db import one
from tests.chatgpt.test_signin import issue_start


async def test_u10_expiry_sends_one_notice(chatgpt_app, slack_client):
    ctx = chatgpt_app[0].state.ctx
    chatgpt_app[1].device = True
    await issue_start(ctx, paste=False)
    async with ctx.pool.connection() as conn:
        await conn.execute(
            "UPDATE connector_oauth_states SET expires_at=clock_timestamp()-interval '1 second'"
        )
    await chatgpt_oauth.poll_devices(ctx)
    await chatgpt_oauth.poll_devices(ctx)
    assert len([c for c in slack_client.calls if c.get("text") == chatgpt_oauth.FAILED]) == 1
    async with ctx.pool.connection() as conn:
        assert (await one(conn, "SELECT consumed_at FROM connector_oauth_states"))["consumed_at"]


@pytest.mark.parametrize("status", [429, 503])
async def test_u10_temporary_poll_keeps_state(chatgpt_app, slack_client, status):
    ctx = chatgpt_app[0].state.ctx
    chatgpt_app[1].device = True
    state, _, _, _ = await issue_start(ctx, paste=False)
    requests = []

    def respond(wire):
        requests.append(wire)
        return httpx.Response(status, json={"error": "slow_down"})

    ctx.chatgpt_client = chatgpt.ChatGPTClient(httpx.MockTransport(respond))
    await chatgpt_oauth.poll_devices(ctx)
    await chatgpt_oauth.poll_devices(ctx)
    assert len(requests) == 1
    assert not slack_client.calls
    async with ctx.pool.connection() as conn:
        row = await one(
            conn, "SELECT * FROM connector_oauth_states WHERE state_hash=%s", (oauth.digest(state),)
        )
        assert row["consumed_at"] is None
        details = json.loads(vault(ctx).decrypt(row["secret"]))
        assert details["interval"] > 5
