from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest


def _load_mcp_module():
    package_root = Path(__file__).resolve().parents[1] / "tools" / "illo-personal-agent-mcp"
    module_path = package_root / "illo_personal_agent_mcp" / "server.py"
    spec = importlib.util.spec_from_file_location("illo_personal_agent_mcp_server", module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_tool_catalog_contains_behavior_guidance():
    module = _load_mcp_module()

    response = module.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    tools = {tool["name"]: tool for tool in response["result"]["tools"]}

    assert {
        "illo_submit_signal",
        "illo_search_workspace",
        "illo_get_thread",
        "illo_create_thread",
        "illo_post_thread_message",
        "illo_ask",
        "illo_get_ask",
        "illo_get_team_members",
    } == set(tools)
    assert "default tool for automatic hooks" in tools["illo_submit_signal"]["description"]
    assert "before creating a new thread" in tools["illo_search_workspace"]["description"]
    assert "without creating a visible thread" in tools["illo_ask"]["description"]
    assert "Advanced compatibility tool" in tools["illo_create_thread"]["description"]


def test_client_routes_and_auth_header_are_stable(monkeypatch):
    module = _load_mcp_module()
    calls: list[dict] = []

    def fake_json_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return {"ok": True}

    monkeypatch.setattr(module, "_json_request", fake_json_request)
    client = module.IlloBridgeClient(module.IlloBridgeConfig("https://illo.test", "bridge-token", 12))

    client.search_workspace("roadmap", limit=5)
    client.submit_signal(
        "Implemented signal submission",
        repo="illospace-project",
        branch="codex/mcp-submit-signal",
        files_touched=[" brain/app/api/routers/agent_mcp.py ", ""],
        metadata={"source": "test"},
    )
    client.get_thread("idea 1", limit=9)
    client.get_team_members()
    client.create_thread(
        "Status",
        "Work shipped",
        teammate_user_ids=[" user-1 ", ""],
        trigger_illo=True,
        metadata={"source": "test"},
    )
    client.post_thread_message("idea/2", "Follow-up")
    client.ask_illo("What context exists?", context={"topic": "roadmap"})
    client.get_ask("ask 1")

    assert [(call["method"], call["url"]) for call in calls] == [
        ("POST", "https://illo.test/api/agent-bridge/workspace/search"),
        ("POST", "https://illo.test/api/mcp"),
        ("GET", "https://illo.test/api/agent-bridge/workspace/threads/idea%201?limit=9"),
        ("GET", "https://illo.test/api/agent-bridge/workspace/team-members"),
        ("POST", "https://illo.test/api/agent-bridge/illo/threads"),
        ("POST", "https://illo.test/api/agent-bridge/illo/threads/idea%2F2/messages"),
        ("POST", "https://illo.test/api/agent-bridge/illo/ask"),
        ("GET", "https://illo.test/api/agent-bridge/illo/ask/ask%201"),
    ]
    assert {call["token"] for call in calls} == {"bridge-token"}
    assert {call["timeout"] for call in calls} == {12}
    assert calls[0]["payload"] == {"query": "roadmap", "limit": 5}
    signal_payload = calls[1]["payload"]
    assert signal_payload["method"] == "tools/call"
    assert signal_payload["params"]["name"] == "illo_submit_signal"
    assert signal_payload["params"]["arguments"]["summary"] == "Implemented signal submission"
    assert signal_payload["params"]["arguments"]["files_touched"] == [
        "brain/app/api/routers/agent_mcp.py"
    ]
    assert calls[4]["payload"]["teammate_user_ids"] == ["user-1"]
    assert calls[4]["payload"]["trigger_illo"] is True
    assert calls[6]["payload"]["context"] == {"topic": "roadmap"}


def test_handle_request_invokes_tool_and_returns_json_text():
    module = _load_mcp_module()
    original = module.TOOLS["illo_search_workspace"]["function"]
    module.TOOLS["illo_search_workspace"]["function"] = lambda **_kwargs: {"results": [{"title": "Roadmap"}]}
    try:
        response = module.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "illo_search_workspace", "arguments": {"query": "roadmap"}},
            }
        )
    finally:
        module.TOOLS["illo_search_workspace"]["function"] = original

    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload == {"results": [{"title": "Roadmap"}]}


def test_handle_request_reports_missing_config(monkeypatch):
    module = _load_mcp_module()
    monkeypatch.delenv("ILLO_BASE_URL", raising=False)
    monkeypatch.delenv("ILLO_BRIDGE_TOKEN", raising=False)

    response = module.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "illo_get_team_members", "arguments": {}},
        }
    )

    assert response["result"]["isError"] is True
    assert "ILLO_BASE_URL is required" in response["result"]["content"][0]["text"]


@pytest.mark.live_provider
def test_live_illo_personal_agent_mcp_smoke(monkeypatch):
    module = _load_mcp_module()
    if not os.environ.get("ILLO_LIVE_MCP_SMOKE"):
        pytest.skip("Set ILLO_LIVE_MCP_SMOKE=1 with ILLO_BASE_URL and ILLO_BRIDGE_TOKEN to run live MCP smoke.")

    monkeypatch.setenv("ILLO_MCP_TIMEOUT", "60")
    result = module.tool_illo_get_team_members()

    assert isinstance(result, dict)
