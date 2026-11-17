"""Exact app guides have no model run. Existing connector keys keep priority."""

from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from illo.channels.mcp import agent_guide
from illo.connectors import mcp_oauth, store
from illo.connectors.config import Auth, Connector
from illo.db import one
from illo.gateway import InboundEvent
from illo.gateway.principal import Authority
from tests.mcp.test_oauth import BASE, Flow


@pytest.fixture
async def guide(app, slack_client, request):
    ctx = app.state.ctx
    mode = getattr(request, "param", "on")
    ctx.workspace.config.connect.public_url = BASE if mode != "no-origin" else None
    ctx.config.vault_key = SecretStr(Fernet.generate_key().decode())
    if mode == "on":
        async with ctx.pool.connection() as conn:
            await store.vault(ctx).set(conn, "slack-client-id", "fake-client")
            await store.vault(ctx).set(conn, "slack-client-secret", "fake-secret")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as client:
        yield Flow(app, client, slack_client), mode


@pytest.mark.parametrize("guide", ["on", "off", "no-origin"], indirect=True)
@pytest.mark.parametrize("command,kind", agent_guide.COMMANDS.items())
async def test_commands_have_three_modes_and_no_brain(guide, command, kind, fake_brain):
    flow, mode = guide
    result = await flow.dm("  " + command.upper() + ".  ")
    assert result["decision"] == "claimed"
    text = next(c["text"] for c in reversed(flow.slack.calls) if c["method"] == "chat_postMessage")
    if mode == "no-origin":
        assert text == "App sign-in is not set up on this Illo. Ask your operator for a token."
    else:
        assert f"`{BASE}/mcp`" in text
        if mode == "on":
            assert "sign in with Slack" in text and "click Allow" in text
        else:
            assert "sign in with Slack" not in text and "click Allow" not in text
            assert "send me the code that the page shows, and click Approve on my card" in text
        if kind == "claude":
            assert "open Customize, then Connectors" in text
            assert 'Choose "Register automatically"' in text
            assert f"`claude mcp add --transport http illo {BASE}/mcp`" in text and "`/mcp`" in text
        elif kind == "codex":
            assert f"`codex mcp add illo --url {BASE}/mcp`" in text
            assert "`codex mcp login illo`" in text
        else:
            assert 'Say "connect my Claude" or "connect my Codex"' in text
    assert not fake_brain.requests
    async with flow.app.state.ctx.pool.connection() as conn:
        assert not await one(conn, "SELECT id FROM runs")


@pytest.mark.parametrize("event,authority", [
    ({"channel": "email"}, Authority("person:ada", "person_dm", 1)),
    ({"kind": "message"}, Authority("person:ada", "person_dm", 1)),
    ({}, Authority("team", "team_channel", 1)),
    ({}, Authority("person:ada", "person_dm", 1, tainted=True)),
    ({}, Authority("outsider", "person_dm")),
    ({"payload": {"bot": True}}, Authority("person:ada", "person_dm", 1)),
    ({"payload": {"webhook": True}}, Authority("person:ada", "person_dm", 1)),
    ({"payload": {"form": True}}, Authority("person:ada", "person_dm", 1)),
])
async def test_dm_only(guide, event, authority):
    flow, _ = guide
    value = InboundEvent(channel="slack", external_id="guide-test", kind="dm", text="connect my claude").model_copy(update=event)
    assert await agent_guide.claim(flow.app.state.ctx, value, authority) is None


@pytest.mark.parametrize("key", ["claude", "codex"])
@pytest.mark.parametrize("prefix", ["connect ", "connect my "])
async def test_connector_key_has_priority(guide, key, prefix, fake_brain, monkeypatch):
    flow, _ = guide
    ctx = flow.app.state.ctx
    ctx.workspace.connectors.append(Connector(key=key, kind="mcp", scope="personal", url="https://tools.example.com/mcp", auth=Auth(type="oauth")))
    reconnect = AsyncMock(return_value="Existing connector sign-in.")
    monkeypatch.setattr(mcp_oauth, "reconnect", reconnect)
    result = await flow.dm(prefix + key)
    assert result["decision"] == "claimed"
    text = next(c["text"] for c in reversed(flow.slack.calls) if c["method"] == "chat_postMessage")
    assert text == "Existing connector sign-in."
    reconnect.assert_awaited_once()
    assert not fake_brain.requests


@pytest.mark.parametrize("command", ["connect my chatgpt", "connect my google", "connect AWS", "connect ABCD-EFGH",
                                      "how do I connect Claude Code to you?", "connect claude again"])
async def test_existing_commands_and_free_text_are_not_claimed(guide, command):
    flow, _ = guide
    value = InboundEvent(channel="slack", external_id="guide-test", kind="dm", text=command)
    assert await agent_guide.claim(flow.app.state.ctx, value, Authority("person:ada", "person_dm", 1)) is None


async def test_free_words_get_generic_command_in_prompt(guide, fake_brain):
    flow, _ = guide
    assert (await flow.dm("how do I connect Claude Code to you?"))["decision"] == "run"
    prompt = fake_brain.requests[0].system_prompt
    assert "`connect my Claude`, `connect my Codex`, or `connect my agent`" in prompt
    assert "claude mcp add" not in prompt and "codex mcp login" not in prompt
