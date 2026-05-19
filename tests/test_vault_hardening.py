"""Regression tests for vault hardening boundaries."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from starlette.testclient import TestClient

from brain.app.api.main import app


USER = {
    "id": "aaaa0000-0000-0000-0000-000000000001",
    "email": "alice@example.test",
    "name": "Alice",
    "role": "owner",
    "org_id": "org00000-0000-0000-0000-000000000001",
    "org_name": "Example",
    "permissions": ["vault:share", "vault:audit"],
}


@contextmanager
def _client():
    from brain.app.api.auth import get_current_user
    from brain.app.api.deps import get_db

    app.dependency_overrides[get_current_user] = lambda: USER
    app.dependency_overrides[get_db] = lambda: MagicMock()
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.clear()


def test_reveal_requires_unlock_when_pin_is_configured():
    with _client() as client, \
         patch("brain.systems.vault.async_has_pin", return_value=True), \
         patch("brain.systems.vault.async_validate_vault_token", return_value=False), \
         patch("brain.systems.vault.async_reveal_secret") as reveal:
        response = client.get("/api/vault/OPENAI_API_KEY")

    assert response.status_code == 423
    reveal.assert_not_called()


def test_list_returns_metadata_without_unlock_when_pin_is_configured():
    secret = {
        "id": 1,
        "key_name": "OPENAI_API_KEY",
        "description": "OpenAI key",
        "category": "api",
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "last_accessed_at": None,
        "access_count": 0,
        "user_id": USER["id"],
        "is_shared": False,
        "shared_by_name": None,
        "agent_access_level": "ask",
    }
    with _client() as client, \
         patch("brain.systems.vault.async_has_pin", return_value=True), \
         patch("brain.systems.vault.async_validate_vault_token", return_value=False), \
         patch("brain.systems.vault.async_list_secrets", return_value=[secret]) as list_secrets:
        response = client.get("/api/vault/")

    assert response.status_code == 200
    payload = response.json()
    assert payload[0]["key_name"] == "OPENAI_API_KEY"
    assert "value" not in payload[0]
    list_secrets.assert_called_once_with(USER["id"], category=None, org_id=USER["org_id"])


def test_unlock_rejects_bad_pin_and_returns_session_token_for_good_pin():
    expires = datetime.now(timezone.utc)
    with _client() as client, \
         patch("brain.systems.vault.async_unlock_vault", side_effect=[None, ("vault-token", expires)]):
        bad = client.post("/api/vault/unlock", json={"pin": "wrong"})
        good = client.post("/api/vault/unlock", json={"pin": "1234"})

    assert bad.status_code == 403
    assert good.status_code == 200
    assert good.json()["token"] == "vault-token"


async def test_brain_vault_requires_user_context():
    from brain.app.mcp.server import tool_brain_vault

    result = await tool_brain_vault("OPENAI_API_KEY")

    assert "authenticated user context" in result["error"]


async def test_vault_secret_prompt_requires_user_context():
    from brain.app.mcp.server import tool_vault_secret_prompt

    result = await tool_vault_secret_prompt("OPENAI_API_KEY")

    assert "authenticated user context" in result["error"]


async def test_vault_inventory_requires_user_context():
    from brain.app.mcp.server import tool_vault_inventory

    result = await tool_vault_inventory()

    assert "authenticated user context" in result["error"]


async def test_vault_secret_prompt_records_missing_and_broadcasts_thread_event():
    from brain.app.mcp.server import tool_vault_secret_prompt

    published = []

    with patch("brain.systems.vault.record_missing_request", new=AsyncMock()) as record_missing, \
         patch("brain.systems.cortex.events.publish_safe", side_effect=lambda event, payload: published.append((event, payload))):
        result = await tool_vault_secret_prompt(
            "example_api_key",
            description="Example API access for generated product workflows.",
            category="api",
            reason="Verify the newly created Example skill against the API.",
            user_id=USER["id"],
            org_id=USER["org_id"],
            run_id=42,
            idea_id="idea-1",
            requested_by="coding-agent",
        )

    assert result["prompted"] is True
    assert result["status"] == "opened"
    assert result["key_name"] == "EXAMPLE_API_KEY"
    assert result["prompt"]["id"].startswith("vault-secret-42-")
    assert result["prompt"]["idea_id"] == "idea-1"
    assert result["prompt"]["key_name"] == "EXAMPLE_API_KEY"
    assert "value" not in result
    assert "secret" not in result
    record_missing.assert_awaited_once_with(
        "EXAMPLE_API_KEY",
        user_id=USER["id"],
        org_id=USER["org_id"],
    )
    assert published
    event_type, payload = published[0]
    assert event_type == "vault_secret_prompt"
    assert payload["idea_id"] == "idea-1"
    assert payload["prompt"]["idea_id"] == "idea-1"
    assert payload["prompt"]["org_id"] == USER["org_id"]
    assert payload["prompt"]["run_id"] == 42
    assert payload["prompt"]["key_name"] == "EXAMPLE_API_KEY"
    assert payload["prompt"]["category"] == "api"
    assert "value" not in payload["prompt"]


async def test_vault_inventory_returns_metadata_without_values():
    from brain.app.mcp.server import tool_vault_inventory

    with patch("brain.systems.vault.async_list_secrets", new=AsyncMock(return_value=[
        {
            "key_name": "GITHUB_TOKEN",
            "description": "GitHub token for agent use.",
            "category": "api",
            "agent_access_level": "available",
            "value": "ghp-secret",
            "encrypted_value": "encrypted-secret",
            "access_count": 3,
        }
    ])) as list_secrets:
        result = await tool_vault_inventory(
            category="api",
            user_id=USER["id"],
            org_id=USER["org_id"],
        )

    assert result["metadata_only"] is True
    assert result["count"] == 1
    assert result["secrets"] == [
        {
            "key_name": "GITHUB_TOKEN",
            "description": "GitHub token for agent use.",
            "category": "api",
            "agent_access_level": "available",
        }
    ]
    assert "value" not in result["secrets"][0]
    assert "encrypted_value" not in result["secrets"][0]
    assert "access_count" not in result["secrets"][0]
    list_secrets.assert_awaited_once_with(
        user_id=USER["id"],
        org_id=USER["org_id"],
        category="api",
    )


async def test_vault_inventory_filters_by_agent_access_level():
    from brain.app.mcp.server import tool_vault_inventory

    with patch("brain.systems.vault.async_list_secrets", new=AsyncMock(return_value=[
        {
            "key_name": "GITHUB_TOKEN",
            "description": "Main GitHub PAT for repo automation.",
            "category": "api",
            "agent_access_level": "available",
        },
        {
            "key_name": "STRIPE_API_KEY",
            "description": "Stripe key requiring explicit approval.",
            "category": "payments",
            "agent_access_level": "ask",
        }
    ])):
        result = await tool_vault_inventory(
            access_level="available",
            user_id=USER["id"],
            org_id=USER["org_id"],
        )

    assert result["count"] == 1
    assert result["secrets"] == [
        {
            "key_name": "GITHUB_TOKEN",
            "description": "Main GitHub PAT for repo automation.",
            "category": "api",
            "agent_access_level": "available",
        }
    ]


async def test_vault_inventory_handler_uses_execution_metadata_context(monkeypatch):
    from brain.systems.runs.execution_context import _agent_context
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_handlers import _get_tool_handlers

    captured = {}

    async def fake_inventory(**kwargs):
        captured.update(kwargs)
        return {"metadata_only": True, "secrets": []}

    monkeypatch.setattr("brain.app.mcp.server.tool_vault_inventory", fake_inventory)

    previous = vars(_agent_context).copy()
    for key in list(vars(_agent_context).keys()):
        delattr(_agent_context, key)
    try:
        with bind_agent_context({
            "execution_metadata": {
                "user_id": "user-from-metadata",
                "org_id": "org-from-metadata",
            },
        }):
            result = await _get_tool_handlers()["vault_inventory"](access_level="available")
    finally:
        for key in list(vars(_agent_context).keys()):
            delattr(_agent_context, key)
        for key, value in previous.items():
            setattr(_agent_context, key, value)

    assert result == {"metadata_only": True, "secrets": []}
    assert captured == {
        "category": None,
        "access_level": "available",
        "user_id": "user-from-metadata",
        "org_id": "org-from-metadata",
    }


async def test_vault_secret_prompt_handler_uses_execution_metadata_context(monkeypatch):
    from brain.systems.runs.execution_context import _agent_context
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_handlers import _get_tool_handlers

    captured = {}

    async def fake_prompt(key_name, **kwargs):
        captured["key_name"] = key_name
        captured.update(kwargs)
        return {"status": "opened"}

    monkeypatch.setattr("brain.app.mcp.server.tool_vault_secret_prompt", fake_prompt)

    previous = vars(_agent_context).copy()
    for key in list(vars(_agent_context).keys()):
        delattr(_agent_context, key)
    try:
        with bind_agent_context({
            "execution_metadata": {
                "user_id": "user-from-metadata",
                "org_id": "org-from-metadata",
                "run_id": 123,
                "target_ref": {"thread_id": "idea-from-target-ref"},
            },
            "worker_name": "codex-worker",
        }):
            result = await _get_tool_handlers()["vault_secret_prompt"]("github_token")
    finally:
        for key in list(vars(_agent_context).keys()):
            delattr(_agent_context, key)
        for key, value in previous.items():
            setattr(_agent_context, key, value)

    assert result == {"status": "opened"}
    assert captured["key_name"] == "github_token"
    assert captured["user_id"] == "user-from-metadata"
    assert captured["org_id"] == "org-from-metadata"
    assert captured["run_id"] == 123
    assert captured["idea_id"] == "idea-from-target-ref"
    assert captured["requested_by"] == "codex-worker"


async def test_brain_vault_requests_grant_before_reading():
    from brain.app.mcp.server import tool_brain_vault

    with patch("brain.systems.vault.authorize_agent_secret_read", new=AsyncMock(return_value={
        "allowed": False,
        "status": "pending",
        "grant": {"id": 123},
    })) as authorize, \
         patch("brain.systems.vault.get_secret", new=AsyncMock()) as get_secret:
        result = await tool_brain_vault(
            "OPENAI_API_KEY",
            reason="Need provider access for this run",
            user_id=USER["id"],
            org_id=USER["org_id"],
            run_id=42,
        )

    assert result == {
        "error": "Vault grant required before this agent can read the secret",
        "grant_id": 123,
        "status": "pending",
    }
    authorize.assert_awaited_once()
    get_secret.assert_not_awaited()


async def test_brain_vault_publishes_grant_prompt_for_thread_approval():
    from brain.app.mcp.server import tool_brain_vault

    requested_at = datetime(2026, 5, 19, 14, 0, tzinfo=timezone.utc)
    grant = {
        "id": 123,
        "key_name": "GITHUB_TOKEN",
        "run_id": 42,
        "requested_by": "illo",
        "reason": "Need GitHub access for this run",
        "requested_at": requested_at,
        "expires_at": datetime(2026, 5, 19, 14, 30, tzinfo=timezone.utc),
    }
    with patch("brain.systems.vault.authorize_agent_secret_read", new=AsyncMock(return_value={
        "allowed": False,
        "status": "pending",
        "grant": grant,
    })), \
         patch("brain.systems.vault.get_secret", new=AsyncMock()) as get_secret, \
         patch("brain.systems.cortex.events.publish_safe") as publish:
        result = await tool_brain_vault(
            "GITHUB_TOKEN",
            reason="Need GitHub access for this run",
            user_id=USER["id"],
            org_id=USER["org_id"],
            run_id=42,
            idea_id="idea-1",
            requested_by="illo",
        )

    assert result["status"] == "pending"
    get_secret.assert_not_awaited()
    publish.assert_called_once()
    event_type, payload = publish.call_args.args
    assert event_type == "vault_agent_grant_prompt"
    assert payload["idea_id"] == "idea-1"
    assert payload["run_id"] == 42
    assert payload["target_user_id"] == USER["id"]
    assert payload["grant"] == {
        **grant,
        "requested_at": requested_at.isoformat(),
        "expires_at": "2026-05-19T14:30:00+00:00",
    }
    assert payload["prompt"]["target_user_id"] == USER["id"]
    assert payload["prompt"]["grant_id"] == 123
    assert payload["prompt"]["key_name"] == "GITHUB_TOKEN"
    assert payload["prompt"]["reason"] == "Need GitHub access for this run"
    assert payload["prompt"]["requested_at"] == requested_at.isoformat()
    assert "value" not in payload["prompt"]


async def test_brain_vault_uses_scoped_agent_read_after_grant():
    from brain.app.mcp.server import tool_brain_vault

    with patch("brain.systems.vault.authorize_agent_secret_read", new=AsyncMock(return_value={"allowed": True, "status": "approved"})), \
         patch("brain.systems.vault.get_secret", new=AsyncMock(return_value="secret-value")) as get_secret:
        result = await tool_brain_vault(
            "OPENAI_API_KEY",
            reason="Need provider access for this run",
            user_id=USER["id"],
            org_id=USER["org_id"],
            run_id=42,
        )

    assert result == {"key": "OPENAI_API_KEY", "value": "secret-value"}
    get_secret.assert_awaited_once_with(
        "OPENAI_API_KEY",
        user_id=USER["id"],
        org_id=USER["org_id"],
        accessed_by="agent",
    )


def test_vault_tool_trace_result_is_redacted():
    from brain.systems.runs.events import redact_tool_call_result

    assert (
        redact_tool_call_result(
            "brain_vault",
            '{"key":"OPENAI_API_KEY","value":"sk-secret"}',
        )
        == "[secret redacted]"
    )
