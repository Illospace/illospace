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

    assert {"illo_submit", "illo_read", "illo_act", "illo_get_result"} == set(tools)
    assert "queues headless handling" in tools["illo_submit"]["description"]
    assert "message" in tools["illo_submit"]["inputSchema"]["properties"]
    assert "desired_outcome" in tools["illo_submit"]["inputSchema"]["properties"]
    assert tools["illo_submit"]["inputSchema"]["required"] == ["message"]
    assert "named capability" in tools["illo_read"]["description"]
    assert tools["illo_read"]["inputSchema"]["required"] == ["capability"]
    assert "project_contexts.search" in tools["illo_read"]["inputSchema"]["properties"]["capability"]["description"]
    assert "user's delegate" in tools["illo_act"]["description"]
    assert tools["illo_act"]["inputSchema"]["required"] == ["capability"]
    assert "result_id" in tools["illo_get_result"]["description"]
    assert tools["illo_get_result"]["inputSchema"]["required"] == []


def test_client_routes_and_auth_header_are_stable(monkeypatch):
    module = _load_mcp_module()
    calls: list[dict] = []

    def fake_json_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        tool_name = kwargs["payload"]["params"]["name"]
        payload = {"tool": tool_name, "result_id": f"{tool_name}-result"}
        if tool_name == "illo_submit":
            payload.update(
                {
                    "thread_id": "idea-1",
                    "thread_url": "https://illo.test/cortex?idea=idea-1",
                    "thread_route": "/cortex?idea=idea-1",
                    "url": "https://illo.test/cortex?idea=idea-1",
                }
            )
        return {"result": {"content": [{"type": "text", "text": json.dumps(payload)}]}}

    monkeypatch.setattr(module, "_json_request", fake_json_request)
    client = module.IlloBridgeClient(module.IlloBridgeConfig("https://illo.test", "bridge-token", 12))

    submit_result = client.submit(
        "Ask the team to review the implementation context",
        desired_outcome="preserve_knowledge",
        parts=[{"type": "text", "text": "Implemented MCP submission"}],
        repo="illospace-project",
        branch="codex/mcp-submit-context",
        files_touched=[" brain/app/api/routers/agent_mcp.py ", ""],
        metadata={"source": "test"},
    )
    read_result = client.read(
        "workspace.search",
        arguments={"query": "roadmap", "limit": 5},
    )
    act_result = client.act(
        "thread.create",
        arguments={
            "title": "Status",
            "body": "Work shipped",
            "teammate_user_ids": ["user-1"],
        },
        reason="Share the shipped status with the team",
        metadata={"source": "test"},
    )
    result = client.get_result("result 1", limit=5)

    assert [(call["method"], call["url"]) for call in calls] == [
        ("POST", "https://illo.test/api/mcp"),
        ("POST", "https://illo.test/api/mcp"),
        ("POST", "https://illo.test/api/mcp"),
        ("POST", "https://illo.test/api/mcp"),
    ]
    assert {call["token"] for call in calls} == {"bridge-token"}
    assert {call["timeout"] for call in calls} == {12}
    assert [call["payload"]["params"]["name"] for call in calls] == [
        "illo_submit",
        "illo_read",
        "illo_act",
        "illo_get_result",
    ]
    context_payload = calls[0]["payload"]
    assert context_payload["method"] == "tools/call"
    assert context_payload["params"]["arguments"]["message"] == "Ask the team to review the implementation context"
    assert context_payload["params"]["arguments"]["desired_outcome"] == "preserve_knowledge"
    assert context_payload["params"]["arguments"]["parts"] == [
        {"type": "text", "text": "Implemented MCP submission"}
    ]
    assert context_payload["params"]["arguments"]["files_touched"] == [
        "brain/app/api/routers/agent_mcp.py"
    ]
    assert submit_result["thread_url"] == "https://illo.test/cortex?idea=idea-1"
    assert submit_result["url"] == submit_result["thread_url"]
    assert submit_result["thread_route"] == "/cortex?idea=idea-1"
    read_arguments = calls[1]["payload"]["params"]["arguments"]
    assert read_arguments == {
        "capability": "workspace.search",
        "arguments": {"query": "roadmap", "limit": 5},
    }
    assert read_result["tool"] == "illo_read"
    act_arguments = calls[2]["payload"]["params"]["arguments"]
    assert act_arguments == {
        "capability": "thread.create",
        "arguments": {
            "title": "Status",
            "body": "Work shipped",
            "teammate_user_ids": ["user-1"],
        },
        "reason": "Share the shipped status with the team",
        "metadata": {"source": "test"},
    }
    assert act_result["tool"] == "illo_act"
    result_arguments = calls[3]["payload"]["params"]["arguments"]
    assert result_arguments == {"result_id": "result 1", "limit": 5}
    assert result["tool"] == "illo_get_result"


def test_handle_request_invokes_tool_and_returns_json_text():
    module = _load_mcp_module()
    original = module.TOOLS["illo_read"]["function"]
    module.TOOLS["illo_read"]["function"] = lambda **_kwargs: {"results": [{"title": "Roadmap"}]}
    try:
        response = module.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "illo_read", "arguments": {"capability": "workspace.search"}},
            }
        )
    finally:
        module.TOOLS["illo_read"]["function"] = original

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
            "params": {"name": "illo_get_result", "arguments": {"result_id": "result-1"}},
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
    result = module.tool_illo_read(capability="team.members.list")

    assert isinstance(result, dict)
