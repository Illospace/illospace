from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

from brain.systems.external_agents import service


def _load_bridge_module():
    bridge_path = Path(__file__).resolve().parents[1] / "tools" / "personal-agent-bridge" / "bridge.py"
    spec = importlib.util.spec_from_file_location("personal_agent_bridge", bridge_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_bridge_token_helpers_are_stable_and_scoped():
    token = service.generate_connection_token()

    assert token.startswith("illo_conn_")
    assert service.token_prefix(token) == token[:18]
    assert service.hash_connection_token(token) == service.hash_connection_token(token)
    assert service.hash_connection_token(token) != service.hash_connection_token(token + "x")
    assert {
        service.SCOPE_TASK_CLAIM,
        service.SCOPE_TASK_COMPLETE,
        service.SCOPE_WORKSPACE_READ,
        service.SCOPE_ILLO_ASK,
        service.SCOPE_ILLO_THREAD_CREATE,
    }.issubset(set(service.DEFAULT_BRIDGE_SCOPES))


def test_headless_thread_ids_use_external_agent_namespace():
    principal = service.AgentBridgePrincipal(
        connection_id="conn-1",
        org_id="org-1",
        owner_user_id="user-1",
        token_id="token-1",
        scopes=frozenset(service.DEFAULT_BRIDGE_SCOPES),
        connection_display_name="Hermes",
        agent_kind="hermes",
    )

    assert f"external-agent:{principal.connection_id}:ask-1" == "external-agent:conn-1:ask-1"


def test_fake_bridge_adapter_echoes_task_without_network():
    module = _load_bridge_module()

    result = module.FakeAdapter().run_task(
        {"id": "task-1", "title": "Share work", "instructions": "Summarize the draft", "input_parts": []}
    )

    assert "Share work" in result["result_summary"]
    assert result["artifacts"][0]["content_json"]["task_id"] == "task-1"


def test_bridge_run_once_claims_runs_and_completes_task_without_network():
    module = _load_bridge_module()

    class StubIlloClient:
        def __init__(self):
            self.calls: list[tuple[str, tuple, dict]] = []

        def heartbeat(self, *args, **kwargs):
            self.calls.append(("heartbeat", args, kwargs))

        def claim(self, *args, **kwargs):
            self.calls.append(("claim", args, kwargs))
            return [
                {
                    "id": "task-1",
                    "title": "Share work",
                    "instructions": "Summarize the draft",
                    "source_surface": "cortex",
                    "input_parts": [],
                }
            ]

        def event(self, *args, **kwargs):
            self.calls.append(("event", args, kwargs))

        def complete(self, *args, **kwargs):
            self.calls.append(("complete", args, kwargs))

        def fail(self, *args, **kwargs):
            self.calls.append(("fail", args, kwargs))

    client = StubIlloClient()

    assert module.run_once(client, module.FakeAdapter(), max_tasks=1) == 1

    call_names = [name for name, _args, _kwargs in client.calls]
    assert call_names == ["heartbeat", "claim", "event", "complete"]
    assert client.calls[-1][1][0] == "task-1"
    assert "Fake personal agent completed" in client.calls[-1][1][1]


def test_hermes_adapter_uses_openai_chat_completions_contract(monkeypatch):
    module = _load_bridge_module()
    calls: list[dict] = []

    def fake_json_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        return {
            "id": "chatcmpl-1",
            "model": "hermes-agent",
            "choices": [{"message": {"content": "Hermes completed it."}}],
            "usage": {"total_tokens": 12},
        }

    monkeypatch.setattr(module, "_json_request", fake_json_request)
    monkeypatch.setenv("HERMES_BASE_URL", "http://hermes.local")
    monkeypatch.setenv("HERMES_API_KEY", "secret")
    monkeypatch.delenv("HERMES_API_MODE", raising=False)
    monkeypatch.delenv("HERMES_RUN_ENDPOINT", raising=False)

    result = module.HermesAdapter().run_task(
        {
            "id": "task-1",
            "connection_id": "conn-1",
            "title": "Share work",
            "instructions": "Summarize the draft",
            "source_surface": "cortex",
            "input_parts": [{"type": "note", "text": "draft"}],
        }
    )

    assert result["result_summary"] == "Hermes completed it."
    assert result["artifacts"][0]["content_json"]["id"] == "chatcmpl-1"
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == "http://hermes.local/v1/chat/completions"
    assert calls[0]["token"] == "secret"
    assert calls[0]["headers"] == {
        "X-Hermes-Session-Id": "illo-task-task-1",
        "X-Hermes-Session-Key": "illo-connection-conn-1",
    }
    payload = calls[0]["payload"]
    assert payload["model"] == "hermes-agent"
    assert payload["stream"] is False
    assert "Summarize the draft" in payload["messages"][1]["content"]


def test_hermes_adapter_can_poll_runs_api(monkeypatch):
    module = _load_bridge_module()
    calls: list[dict] = []

    def fake_json_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        if method == "POST":
            return {"run_id": "run-1", "status": "started"}
        return {
            "run_id": "run-1",
            "status": "completed",
            "session_id": "illo-task-task-1",
            "output": "Run output",
            "usage": {"total_tokens": 8},
        }

    monkeypatch.setattr(module, "_json_request", fake_json_request)
    monkeypatch.setenv("HERMES_BASE_URL", "http://hermes.local")
    monkeypatch.setenv("HERMES_API_KEY", "secret")
    monkeypatch.setenv("HERMES_API_MODE", "runs")
    monkeypatch.delenv("HERMES_RUN_ENDPOINT", raising=False)

    result = module.HermesAdapter().run_task({"id": "task-1", "title": "Share work", "instructions": "Do it"})

    assert result["result_summary"] == "Run output"
    assert result["artifacts"][0]["content_json"]["run_id"] == "run-1"
    assert calls[0]["url"] == "http://hermes.local/v1/runs"
    assert calls[1]["url"] == "http://hermes.local/v1/runs/run-1"


@pytest.mark.live_provider
def test_live_hermes_runs_adapter_smoke(monkeypatch):
    if os.environ.get("ILLO_LIVE_HERMES_SMOKE") != "1":
        pytest.skip("Set ILLO_LIVE_HERMES_SMOKE=1 with HERMES_BASE_URL and HERMES_API_KEY to run live Hermes smoke.")
    if not os.environ.get("HERMES_BASE_URL"):
        pytest.skip("HERMES_BASE_URL is required for live Hermes smoke.")

    module = _load_bridge_module()
    monkeypatch.setenv("HERMES_API_MODE", "runs")
    monkeypatch.setenv("HERMES_RUN_POLL_INTERVAL", os.environ.get("HERMES_RUN_POLL_INTERVAL", "1"))
    monkeypatch.setenv("PERSONAL_AGENT_TIMEOUT", os.environ.get("PERSONAL_AGENT_TIMEOUT", "180"))

    result = module.HermesAdapter().run_task(
        {
            "id": "live-hermes-smoke",
            "connection_id": "live-connection",
            "title": "Hermes live bridge smoke",
            "instructions": "Reply exactly with ILLO_HERMES_LIVE_OK and one short sentence.",
            "source_surface": "ci_live_smoke",
            "input_parts": [{"type": "smoke", "source": "pytest"}],
        }
    )

    assert "ILLO_HERMES_LIVE_OK" in result["result_summary"]
