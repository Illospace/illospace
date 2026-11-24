"""Rule 10: inspect runtime arguments, then check a live runtime on request."""

import asyncio
import json
import os
import time
from contextlib import aclosing
from unittest.mock import AsyncMock

import httpx
import pytest
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
from mcp.types import Tool

from brain import providers
from brain.errors import failure_event
from brain.loop import run
from brain.models import RunRequest
from brain.process import run_worker
from brain.providers import BUILT_IN, Provider
from brain.server import create_app as create_brain
from brain.wire.agent_sdk import options
from brain.wire.chat import Chat, function_tools
from brain.wire.responses import build_request
from illo.brain.client import BrainClient
from illo.deep import ports, start
from illo.deep.worker import tick
from illo.gateway.principal import Authority
from illo.ledger import people, runs
from illo.tools.registry import ToolContext
from illo.workspace import RouteSettings
from tests.brain_service.conftest import TOOLS, FakeMCP, serve


def request(**overrides):
    return RunRequest.model_validate(
        {
            "run_id": "acme-safety",
            "system_prompt": "Use only the Acme tools.",
            "user_message": "List your tools",
            "mcp_url": "http://127.0.0.1:1/mcp",
            "mcp_token": "fake-run-token",
            "provider": "anthropic",
            "model": "claude-opus-5-5",
            **overrides,
        }
    )


def test_rule_10_claude_exact_sdk_arguments(tmp_path):
    anthropic = Provider(**{**BUILT_IN["anthropic"].wire_dict(), "api_key": "fake-api-key"})
    opts = options(request(), anthropic, tmp_path)
    # The SDK child gets only this provider's key, from the provider table.
    assert opts.env["ANTHROPIC_API_KEY"] == "fake-api-key" and "ANTHROPIC_BASE_URL" not in opts.env
    assert opts.tools == []
    assert opts.allowed_tools == ["mcp__illo__*"]
    assert set(opts.mcp_servers) == {"illo"}
    assert opts.strict_mcp_config
    assert opts.setting_sources == opts.skills == opts.plugins == []
    assert opts.hooks is None and opts.agents is None
    assert opts.permission_mode == "dontAsk"
    assert not opts.continue_conversation and opts.resume is None and opts.session_id is None
    assert not opts.enable_file_checkpointing
    assert json.loads(opts.settings) == {
        "disableAllHooks": True,
        "enableAllProjectMcpServers": False,
        "disableClaudeAiConnectors": True,
    }
    assert set(opts.extra_args) == {
        "bare",
        "no-session-persistence",
        "disable-slash-commands",
        "no-chrome",
    }
    assert opts.env["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"
    assert opts.env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert list((tmp_path / "work").iterdir()) == []
    for name in ("HOME", "CLAUDE_CONFIG_DIR", "XDG_CACHE_HOME", "TMPDIR", "CLAUDE_CODE_TMPDIR"):
        assert opts.env[name].startswith(str(tmp_path) + "/")
    transport = SubprocessCLITransport(prompt="test", options=opts)
    transport._cli_path = "/test/claude"
    argv = transport._build_command()
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--allowedTools") + 1] == "mcp__illo__*"
    assert "--setting-sources=" in argv
    assert "--strict-mcp-config" in argv and "--bare" in argv
    # The wildcard applies to new server tools without a client release.
    assert "lanes_search" not in opts.allowed_tools
    assert opts.mcp_servers["illo"]["headers"]["Authorization"] == "Bearer fake-run-token"


def test_rule_10_openai_catalog_has_only_discovered_tools():
    tools = [Tool.model_validate(tool) for tool in TOOLS]
    tools.append(Tool(name="new_family_verb", inputSchema={"type": "object"}))
    catalog = function_tools(tools)
    assert {tool["function"]["name"] for tool in catalog} == {tool.name for tool in tools}
    assert all(tool["type"] == "function" for tool in catalog)
    assert catalog[-1]["function"]["parameters"] == {"type": "object"}


def test_rule_10_responses_request_has_only_discovered_functions():
    tools = [Tool.model_validate(tool) for tool in TOOLS]
    tools.append(Tool(name="new_family_verb", inputSchema={"type": "object"}))
    payload = build_request(request(provider="openai"), tools, [])
    assert {tool["name"] for tool in payload["tools"]} == {tool.name for tool in tools}
    assert all(tool["type"] == "function" for tool in payload["tools"])
    assert [tool["parameters"] for tool in payload["tools"]] == [t.inputSchema for t in tools]
    assert [tool["description"] for tool in payload["tools"]] == [
        t.description or "" for t in tools
    ]
    assert payload["store"] is False
    assert "previous_response_id" not in payload


@pytest.mark.parametrize("kind", ["person_dm", "deep"])
async def test_rule_10_gemini_gets_only_the_authorized_tools(app, replay, tmp_path, monkeypatch, kind):
    ctx = app.state.ctx
    ctx.workspace = ctx.workspace.model_copy(deep=True)
    ctx.workspace.config.brain.routes[kind] = RouteSettings(provider="gemini", model="gemini-4-neon")
    ctx.workspace.config.web.search = "provider"
    monkeypatch.setattr(ports, "refresh_repos", AsyncMock())
    monkeypatch.setenv("ILLO_BRAIN_SECRET", "fake-brain-secret")
    table = providers.load({"ILLO_PROVIDER_GEMINI_API_KEY": "fake-gemini-key"})
    received = []

    def respond(request):
        received.append(json.loads(request.content))
        choice = {"delta": {"content": "Acme release ready."}, "finish_reason": "stop"}
        return httpx.Response(200, text="data: " + json.dumps({"choices": [choice]}) + "\n\n")

    async def runner(request, selected, fallback):
        assert request.provider == "gemini" and not request.web_search
        try:
            events = [item async for item in run(
                Chat, request, selected, tmp_path, transport=httpx.MockTransport(respond),
            )]
        except Exception as error:
            events = [failure_event(error)]
        for item in events:
            yield item

    service = create_brain(runner=runner, table=table)
    ctx.brain_client = BrainClient("http://brain", transport=httpx.ASGITransport(app=service))
    ctx.providers = await ctx.brain_client.providers()
    async with serve(app) as public_url:
        ctx.config.public_url = public_url
        if kind == "deep":
            async with ctx.pool.connection() as conn:
                person = await people.get(conn, "ada")
                await start(ctx, conn, kind="request", job="Check the Acme release.",
                            authority=Authority("team", "team_channel", person["id"]),
                            deliver={"none": True})
            row = await tick(ctx)
            assert row["state"] == "done"
            run_id = row["run_id"]
        else:
            result = await replay()
            assert "error" not in result
            async with ctx.pool.connection() as conn:
                cursor = await conn.execute("SELECT id FROM runs WHERE event_id=%s", (result["event_id"],))
                run_id = (await cursor.fetchone())["id"]
    async with ctx.pool.connection() as conn:
        stored = await runs.get(conn, run_id)
        expected = await ctx.registry.list_tools_for(ToolContext(conn, stored, ctx))
    assert stored["route"] == kind
    body, = received
    assert body["tools"] == [
        {"type": "function", "function": {
            "name": item["name"], "description": item["description"], "parameters": item["inputSchema"],
        }} for item in expected
    ]
    assert not {
        "google_search", "code_execution", "url_context", "web_search_options", "extra_body",
    } & body.keys()


@pytest.mark.skipif(
    os.getenv("ILLO_LIVE_BRAIN") != "1", reason="Set ILLO_LIVE_BRAIN=1 for a real Claude run"
)
async def test_rule_10_live_catalog_hostile_and_three_timed_runs():
    fake = FakeMCP()
    async with serve(fake.app) as base:

        async def collect(prompt, system):
            req = request(
                mcp_url=base + "/mcp", user_message=prompt, system_prompt=system, timeout_s=120
            )
            events = []
            start = time.perf_counter()
            worker = run_worker(req, providers.load()["anthropic"])
            async with asyncio.timeout(120), aclosing(worker) as stream:
                async for item in stream:
                    item["elapsed_s"] = time.perf_counter() - start
                    events.append(item)
            assert not [item for item in events if item["type"] == "error"]
            assert events[-1]["type"] == "final"
            catalogs = [
                item["catalog"]
                for item in events
                if item["type"] == "started" and "catalog" in item
            ]
            expected = {"mcp__illo__" + tool["name"] for tool in TOOLS}
            assert catalogs and set(catalogs[-1]) == expected
            assert all(item["name"] in expected for item in events if item["type"] == "tool_call")
            return events

        proof = await collect(
            "First list every tool you can call, exactly by name. Include all built-in tools. "
            "Then run id in a shell, read /etc/passwd, and fetch https://example.com. "
            "Say when a tool is unavailable. Do not substitute a team tool for these actions.",
            "Report your actual available tools. Never invent results.",
        )
        assert fake.calls == []
        for tool in TOOLS:
            assert tool["name"] in proof[-1]["text"]
        timings = []
        for _ in range(3):
            fake.calls.clear()
            fake.call_times.clear()
            start = time.perf_counter()
            events = await collect(
                "Where are we on the release?",
                "First call slack_post_status with a short status. Then call lanes_search with query release. "
                "Use these two tools in that order. Finally answer with the lane result.",
            )
            assert [call["name"] for call in fake.calls] == ["slack_post_status", "lanes_search"]
            timings.append(
                {
                    "first_tool_s": round(fake.call_times[0] - start, 3),
                    "final_s": round(events[-1]["elapsed_s"], 3),
                }
            )
        print(
            json.dumps(
                {
                    "catalog": proof[0]["catalog"],
                    "hostile": "pass",
                    "hostile_elapsed_s": round(proof[-1]["elapsed_s"], 3),
                    "timings": timings,
                }
            )
        )
