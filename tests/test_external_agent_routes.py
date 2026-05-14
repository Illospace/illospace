from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from brain.app.api.auth import get_current_user
from brain.app.api.deps import get_db, rate_limit
from brain.app.api.main import app
from brain.systems.external_agents import service as external_agents


pytestmark = pytest.mark.asyncio


class _AsyncSession:
    def __init__(self, order: list[str] | None = None):
        self.sync_session = MagicMock()
        self.order = order if order is not None else []

    async def run_sync(self, fn):
        return fn(self.sync_session)

    async def commit(self):
        self.order.append("commit")

    async def rollback(self):
        self.order.append("rollback")

    async def close(self):
        return None


def _principal() -> external_agents.AgentBridgePrincipal:
    return external_agents.AgentBridgePrincipal(
        connection_id="conn-1",
        org_id="org-1",
        owner_user_id="user-1",
        token_id="token-1",
        scopes=frozenset(external_agents.DEFAULT_BRIDGE_SCOPES),
        connection_display_name="Hermes",
        agent_kind="hermes",
    )


async def _request(method: str, path: str, *, user: dict | None = None, session: _AsyncSession | None = None, **kwargs):
    overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = lambda: session or _AsyncSession()
    app.dependency_overrides[get_current_user] = lambda: user or {
        "id": "user-1",
        "org_id": "org-1",
        "role": "member",
    }
    app.dependency_overrides[rate_limit] = lambda: None
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            return await client.request(method, path, **kwargs)
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(overrides)


async def test_member_cannot_mint_token_for_connection_they_do_not_manage():
    with patch(
        "brain.app.api.routers.agent_connections.external_agents.require_connection_for_user",
        side_effect=external_agents.ExternalAgentPermissionError("Permission denied"),
    ) as require_connection, patch(
        "brain.app.api.routers.agent_connections.external_agents.mint_connection_token"
    ) as mint_token:
        response = await _request(
            "POST",
            "/api/agent-connections/conn-1/tokens",
            user={"id": "user-2", "org_id": "org-1", "role": "member"},
            json={},
        )

    assert response.status_code == 403
    require_connection.assert_called_once()
    mint_token.assert_not_called()


async def test_connection_listing_is_owner_scoped_for_members_and_org_wide_for_admins():
    captured: list[dict] = []

    def list_connections(_sync_db, **kwargs):
        captured.append(kwargs)
        return []

    with patch(
        "brain.app.api.routers.agent_connections.external_agents.list_connections",
        side_effect=list_connections,
    ):
        member_response = await _request(
            "GET",
            "/api/agent-connections",
            user={"id": "user-2", "org_id": "org-1", "role": "member"},
        )
        admin_response = await _request(
            "GET",
            "/api/agent-connections",
            user={"id": "admin-1", "org_id": "org-1", "role": "admin"},
        )

    assert member_response.status_code == 200
    assert admin_response.status_code == 200
    assert captured[0] == {"org_id": "org-1", "owner_user_id": "user-2"}
    assert captured[1] == {"org_id": "org-1", "owner_user_id": None}


async def test_bridge_complete_commits_before_broadcasting_thread_message():
    order: list[str] = []
    session = _AsyncSession(order)
    task = SimpleNamespace(id="task-1")
    message = SimpleNamespace(
        id=7,
        idea_id="idea-1",
        role="illo",
        content="Done",
        attachments=[],
        metadata_={},
        user_id=None,
        message_type="agent_response",
        created_at=datetime.now(timezone.utc),
    )

    async def broadcast(event_type, payload, **_kwargs):
        order.append(f"broadcast:{event_type}")

    with patch(
        "brain.app.api.routers.agent_bridge.external_agents.authenticate_bridge_token",
        return_value=_principal(),
    ), patch(
        "brain.app.api.routers.agent_bridge.external_agents.complete_task",
        return_value=(task, message),
    ), patch(
        "brain.app.api.routers.agent_bridge.external_agents.serialize_task",
        return_value={"id": "task-1", "status": "completed"},
    ), patch(
        "brain.app.api.routers.agent_bridge.external_agents.serialize_thread_message",
        return_value={"id": 7, "idea_id": "idea-1", "content": "Done"},
    ), patch(
        "brain.app.api.routers.agent_bridge.ws_manager.broadcast_product_event",
        new=AsyncMock(side_effect=broadcast),
    ):
        response = await _request(
            "POST",
            "/api/agent-bridge/tasks/task-1/complete",
            session=session,
            headers={"Authorization": "Bearer bridge-token"},
            json={"result_summary": "Done"},
        )

    assert response.status_code == 200
    assert order[:2] == ["commit", "broadcast:thread_message"]


async def test_cortex_external_agent_task_commits_before_broadcasting_delegation_message():
    order: list[str] = []
    session = _AsyncSession(order)
    task = SimpleNamespace(id="task-1")
    message = SimpleNamespace(
        id=9,
        idea_id="idea-1",
        role="illo",
        content="Delegated",
        attachments=[],
        metadata_={},
        user_id=None,
        message_type="agent_status",
        created_at=datetime.now(timezone.utc),
    )

    async def broadcast(event_type, payload, **_kwargs):
        order.append(f"broadcast:{event_type}")

    with patch(
        "brain.app.api.routers.cortex._external_agents.external_agents.create_external_task_for_idea",
        return_value=(task, message),
    ), patch(
        "brain.app.api.routers.cortex._external_agents.external_agents.serialize_task",
        return_value={"id": "task-1", "status": "queued"},
    ), patch(
        "brain.app.api.routers.cortex._external_agents.external_agents.serialize_thread_message",
        return_value={"id": 9, "idea_id": "idea-1", "content": "Delegated"},
    ), patch(
        "brain.app.api.routers.cortex._external_agents.ws_manager.broadcast_product_event",
        new=AsyncMock(side_effect=broadcast),
    ):
        response = await _request(
            "POST",
            "/api/cortex/ideas/idea-1/external-agent-tasks",
            session=session,
            user={"id": "user-1", "org_id": "org-1", "role": "member"},
            json={"connection_id": "conn-1", "instructions": "Please review this"},
        )

    assert response.status_code == 201
    assert order[:3] == ["commit", "broadcast:thread_message", "broadcast:status_change"]


async def test_hosted_mcp_lists_tools_for_scoped_bridge_token():
    with patch(
        "brain.app.api.routers.agent_mcp.external_agents.authenticate_bridge_token",
        return_value=_principal(),
    ):
        response = await _request(
            "POST",
            "/mcp",
            headers={"Authorization": "Bearer bridge-token"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )

    assert response.status_code == 200
    names = {tool["name"] for tool in response.json()["result"]["tools"]}
    assert "illo_create_thread" in names
    assert "illo_ask" in names
    assert "illo_search_workspace" in names


async def test_hosted_mcp_filters_tools_by_bridge_token_scope():
    principal = external_agents.AgentBridgePrincipal(
        connection_id="conn-1",
        org_id="org-1",
        owner_user_id="user-1",
        token_id="token-1",
        scopes=frozenset({external_agents.SCOPE_WORKSPACE_READ}),
        connection_display_name="Hermes",
        agent_kind="hermes",
    )

    with patch(
        "brain.app.api.routers.agent_mcp.external_agents.authenticate_bridge_token",
        return_value=principal,
    ):
        response = await _request(
            "POST",
            "/api/mcp",
            headers={"Authorization": "Bearer bridge-token"},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )

    names = {tool["name"] for tool in response.json()["result"]["tools"]}
    assert names == {"illo_search_workspace", "illo_get_thread", "illo_get_team_members"}


async def test_hosted_mcp_create_thread_commits_before_broadcasting():
    order: list[str] = []
    session = _AsyncSession(order)
    idea = SimpleNamespace(
        id="idea-1",
        title="Shared work",
        status="active",
        origin="external_agent",
        origin_ref=None,
    )
    message = SimpleNamespace(
        id=10,
        idea_id="idea-1",
        role="user",
        content="Shared from Hermes",
        attachments=[],
        metadata_={},
        user_id="user-1",
        message_type="message",
        created_at=datetime.now(timezone.utc),
    )

    async def broadcast(event_type, payload, **_kwargs):
        order.append(f"broadcast:{event_type}")

    with patch(
        "brain.app.api.routers.agent_mcp.external_agents.authenticate_bridge_token",
        return_value=_principal(),
    ), patch(
        "brain.app.api.routers.agent_mcp.external_agents.create_thread_from_agent",
        return_value=(idea, message, ["user-2"]),
    ), patch(
        "brain.app.api.routers.agent_mcp._run_trigger_if_requested",
        return_value=None,
    ), patch(
        "brain.app.api.routers.agent_bridge.ws_manager.broadcast_product_event",
        new=AsyncMock(side_effect=broadcast),
    ):
        response = await _request(
            "POST",
            "/mcp",
            session=session,
            headers={"Authorization": "Bearer bridge-token"},
            json={
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "illo_create_thread",
                    "arguments": {"title": "Shared work", "body": "Shared from Hermes"},
                },
            },
        )

    assert response.status_code == 200
    payload = response.json()
    content = payload["result"]["content"][0]["text"]
    assert json.loads(content)["idea"]["id"] == "idea-1"
    assert order[:2] == ["commit", "broadcast:thread_message"]
