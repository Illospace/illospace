from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from brain.systems.external_agents import service


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
    bridge_path = Path(__file__).resolve().parents[1] / "tools" / "personal-agent-bridge" / "bridge.py"
    spec = importlib.util.spec_from_file_location("personal_agent_bridge", bridge_path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    result = module.FakeAdapter().run_task(
        {"id": "task-1", "title": "Share work", "instructions": "Summarize the draft", "input_parts": []}
    )

    assert "Share work" in result["result_summary"]
    assert result["artifacts"][0]["content_json"]["task_id"] == "task-1"
