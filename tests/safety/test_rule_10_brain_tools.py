"""Rule 10: inspect runtime arguments, then check a live runtime on request."""

import asyncio
import json
import os
import time
from contextlib import aclosing

import pytest
from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
from mcp.types import Tool

from brain.models import RunRequest
from brain.process import run_worker
from brain.runtimes.claude import options
from brain.runtimes.openai_compat import function_tools
from tests.brain_service.conftest import TOOLS, FakeMCP, serve


def request(**overrides):
    return RunRequest.model_validate(
        {
            "run_id": "acme-safety",
            "system_prompt": "Use only the Acme tools.",
            "user_message": "List your tools",
            "mcp_url": "http://127.0.0.1:1/mcp",
            "mcp_token": "fake-run-token",
            "runtime": "claude",
            **overrides,
        }
    )


def test_rule_10_claude_exact_sdk_arguments(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-api-key")
    opts = options(request(), tmp_path)
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
            async with asyncio.timeout(120), aclosing(run_worker(req)) as stream:
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
