"""Exact app guides have no model run. Existing connector keys keep priority."""

import json
from unittest.mock import AsyncMock

import httpx
import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from illo.brain.server import tool_result
from illo.channels.mcp import agent_guide
from illo.connectors import mcp_oauth, store
from illo.connectors.config import Auth, Connector
from illo.db import one
from illo.gateway import InboundEvent
from illo.gateway.principal import Authority
from illo.ledger import runs
from illo.tools.connect import STATUS
from illo.tools.registry import MAX_RESULT_CHARS, Registry, ToolContext
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
            assert "`claude mcp remove illo`" in text
        elif kind == "codex":
            assert f"`codex mcp add illo --url {BASE}/mcp`" in text
            assert "`codex mcp login illo`" in text
            assert "`codex mcp remove illo`" in text
        else:
            assert 'Say "connect my Claude" or "connect my Codex"' in text
        if kind in {"claude", "codex"}:
            assert "Run commands in a normal terminal, not inside an agent session" in text
            assert "because its sandbox can block the network." in text
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


@pytest.mark.parametrize("question,kind", [
    ("how do I connect Claude Code to you?", "claude"),
    ("how do I plug my Claude into you?", "claude"),
    ("How does Ben connect his Codex to you? Give the steps and address.", "codex"),
    ("Send Ben the steps to connect his Codex.", "codex"),
])
async def test_free_words_get_live_guide_instruction_in_prompt(guide, fake_brain, question, kind):
    flow, _ = guide
    fake_brain.script = [{"name": "connect_guide", "arguments": {"app": kind}}]
    assert (await flow.dm(question))["decision"] == "run"
    prompt = fake_brain.requests[0].system_prompt
    assert "call `connect_guide` and give its steps" in prompt
    assert "anyone's app or agent connection to Illo" in prompt
    assert "Never use memory or notes." in prompt
    assert "claude mcp add" not in prompt and "codex mcp login" not in prompt
    result = json.loads(fake_brain.results[0]["content"][0]["text"])
    assert result["decision"] == "allow"
    assert f"`{BASE}/mcp`" in result["result"]
    assert f"`{kind} mcp remove illo`" in result["result"]


@pytest.mark.parametrize("guide", ["on", "off", "no-origin"], indirect=True)
@pytest.mark.parametrize("kind", ["claude", "codex", "agent"])
async def test_tool_matches_dm_in_every_profile_and_mode(guide, kind):
    flow, _ = guide
    ctx = flow.app.state.ctx
    assert (await flow.dm("connect my " + kind))["decision"] == "claimed"
    text = next(c["text"] for c in reversed(flow.slack.calls) if c["method"] == "chat_postMessage")
    async with ctx.pool.connection() as conn:
        person = await one(conn, "SELECT id FROM people WHERE key='ada'")
        for profile in sorted(STATUS):
            personal = profile in {"person_dm", "agent_request"}
            run = await runs.create(
                conn, principal="person:ada" if personal else "team",
                person_id=person["id"] if personal else None,
                profile=profile, runtime="fake", model="fake",
            )
            result = await ctx.registry.call(ToolContext(conn, run, ctx), "connect_guide", {"app": kind})
            assert result["decision"] == "allow", result
            assert result["result"] == text
            envelope = tool_result(result, brain=True)
            assert json.loads(envelope["content"][0]["text"])["result"] == text
            assert "result" not in envelope["structuredContent"]
            assert len(envelope["content"][0]["text"]) <= MAX_RESULT_CHARS
            assert not any(c in text for c in ("*", "<", ">", "•", "“", "”", "’"))
            saved = await runs.get(conn, run["id"])
            assert not saved["outside_read"] and not saved["personal_read"]
            assert not saved["customer_read"]
            if kind == "agent":
                default = await ctx.registry.call(ToolContext(conn, run, ctx), "connect_guide", {})
                assert default == result


@pytest.mark.parametrize("profile", [*sorted(STATUS), "outsider", "tainted"])
def test_guide_grant_and_dm_permission(profile):
    from illo.channels.mcp import TOOLS

    registry = Registry()
    registry.discover()
    run = {"principal": "outsider" if profile == "outsider" else "person:ada",
           "profile": profile, "person_id": 1, "tainted": profile == "tainted",
           "personal_read": False}
    names = {tool["name"] for tool in registry.list_tools(run)}
    assert ("connect_guide" in names) == (profile in STATUS)
    assert ("connect_status" in names) == (profile in STATUS)
    if profile == "person_dm":
        assert {"connect_guide", "slack_send_dm"} <= names
    if profile == "agent_request":
        assert "connect_guide" in names and "slack_send_dm" not in names
    assert "connect_guide" not in TOOLS
    tool = registry.tools["connect_guide"]
    assert tool.profiles == registry.tools["connect_status"].profiles == STATUS
    assert tool.risk == "read" and tool.family == "connect"
    assert tool.destination({}) == "ledger" and not tool.personal


@pytest.mark.parametrize("args", [{"app": "chatgpt"}, {"app": None}, {"person": "ben"}])
async def test_guide_rejects_invalid_arguments(guide, args):
    flow, _ = guide
    ctx = flow.app.state.ctx
    async with ctx.pool.connection() as conn:
        person = await one(conn, "SELECT id FROM people WHERE key='ada'")
        run = await runs.create(conn, principal="person:ada", person_id=person["id"],
                                profile="person_dm", runtime="fake", model="fake")
        result = await ctx.registry.call(ToolContext(conn, run, ctx), "connect_guide", args)
        assert result["decision"] == "deny"
        assert result["reason"] == "The tool arguments do not match its schema"


@pytest.mark.parametrize("principal,tainted", [("outsider", False), ("person:ada", True)])
async def test_guide_denies_outside_runs_with_a_person_profile(guide, principal, tainted):
    flow, _ = guide
    ctx = flow.app.state.ctx
    async with ctx.pool.connection() as conn:
        person = await one(conn, "SELECT id FROM people WHERE key='ada'")
        run = await runs.create(
            conn, principal=principal, person_id=None if principal == "outsider" else person["id"],
            profile="person_dm", tainted=tainted, runtime="fake", model="fake",
        )
        assert "connect_guide" not in {t["name"] for t in ctx.registry.list_tools(run)}
        result = await ctx.registry.call(ToolContext(conn, run, ctx), "connect_guide", {})
        assert result["decision"] == "deny" and result["result"] is None
