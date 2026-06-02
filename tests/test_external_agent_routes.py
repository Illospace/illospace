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


def _handoff_row(**overrides):
    data = {
        "id": "88888888-8888-4888-8888-888888888888",
        "org_id": "org-1",
        "created_by_user_id": "11111111-1111-4111-8111-111111111111",
        "source_surface": "slack",
        "source_ref": {"channel_id": "C1"},
        "target_tool": "codex",
        "title": "Launch in Codex",
        "summary": "A prepared Codex handoff.",
        "instructions": "Fetch the full context from Illo and implement the task.",
        "acceptance_criteria": ["Works from Slack and webapp."],
        "context_parts": [{"type": "text", "text": "Relevant context."}],
        "repo_origin_url": "git@github.com:uwear-ai/illospace-project.git",
        "branch_hint": None,
        "status": "open",
        "launch_count": 0,
        "last_launched_by_user_id": None,
        "last_launched_at": None,
        "expires_at": None,
        "idempotency_key": None,
        "metadata_": {},
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    data.update(overrides)
    return SimpleNamespace(**data)


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


async def test_connection_create_reuses_existing_personal_agent_connection():
    session = _AsyncSession()
    connection = SimpleNamespace(
        id="conn-1",
        org_id="org-1",
        owner_user_id="user-1",
        display_name="Codex Desktop",
        agent_kind="codex",
        transport="hosted_mcp",
        status="configured",
        endpoint_url="http://127.0.0.1:8080/mcp",
        remote_agent_id=None,
        remote_session_key=None,
        remote_agent_card={},
        capabilities={"mcp": True},
        last_seen_at=None,
        last_tested_at=None,
        last_error=None,
        metadata_={},
        disabled_at=None,
        created_at=None,
        updated_at=None,
    )

    with patch(
        "brain.app.api.routers.agent_connections.external_agents.find_reusable_connection",
        return_value=connection,
    ) as find_connection, patch(
        "brain.app.api.routers.agent_connections.external_agents.create_connection",
    ) as create_connection:
        response = await _request(
            "POST",
            "/api/agent-connections",
            session=session,
            json={
                "display_name": "Codex Desktop",
                "agent_kind": "codex",
                "transport": "hosted_mcp",
                "endpoint_url": "http://127.0.0.1:8080/mcp",
                "capabilities": {"mcp": True},
            },
        )

    assert response.status_code == 201
    assert response.json()["id"] == "conn-1"
    find_connection.assert_awaited_once_with(
        session,
        org_id="org-1",
        owner_user_id="user-1",
        display_name="Codex Desktop",
        agent_kind="codex",
        transport="hosted_mcp",
    )
    create_connection.assert_not_called()


async def test_connection_tokens_can_be_listed_for_managed_connection():
    session = _AsyncSession()
    token = SimpleNamespace(
        id="token-1",
        connection_id="conn-1",
        token_prefix="illo_conn_abc",
        name="Codex MCP token",
        scopes=[],
        created_at=None,
        last_used_at=None,
        expires_at=None,
        revoked_at=None,
    )

    with patch(
        "brain.app.api.routers.agent_connections.external_agents.require_connection_for_user",
        return_value=SimpleNamespace(id="conn-1"),
    ) as require_connection, patch(
        "brain.app.api.routers.agent_connections.external_agents.list_connection_tokens",
        return_value=[token],
    ) as list_tokens:
        response = await _request(
            "GET",
            "/api/agent-connections/conn-1/tokens",
            session=session,
        )

    assert response.status_code == 200
    assert response.json()[0]["id"] == "token-1"
    require_connection.assert_awaited_once_with(
        session,
        connection_id="conn-1",
        org_id="org-1",
        user_id="user-1",
        role="member",
        require_manage=True,
    )
    list_tokens.assert_awaited_once_with(
        session,
        connection_id="conn-1",
        org_id="org-1",
    )


async def test_connection_token_can_be_revoked_for_managed_connection():
    session = _AsyncSession()
    token = SimpleNamespace(
        id="token-1",
        connection_id="conn-1",
        token_prefix="illo_conn_abc",
        name="Codex MCP token",
        scopes=[],
        created_at=None,
        last_used_at=None,
        expires_at=None,
        revoked_at=datetime.now(timezone.utc),
    )

    with patch(
        "brain.app.api.routers.agent_connections.external_agents.require_connection_for_user",
        return_value=SimpleNamespace(id="conn-1"),
    ) as require_connection, patch(
        "brain.app.api.routers.agent_connections.external_agents.revoke_connection_token",
        return_value=token,
    ) as revoke_token:
        response = await _request(
            "DELETE",
            "/api/agent-connections/conn-1/tokens/token-1",
            session=session,
        )

    assert response.status_code == 200
    assert response.json()["revoked_at"] is not None
    require_connection.assert_awaited_once_with(
        session,
        connection_id="conn-1",
        org_id="org-1",
        user_id="user-1",
        role="member",
        require_manage=True,
    )
    revoke_token.assert_awaited_once_with(
        session,
        connection_id="conn-1",
        token_id="token-1",
        org_id="org-1",
    )


async def test_connection_can_be_removed_for_managed_connection():
    session = _AsyncSession()
    connection = SimpleNamespace(
        id="conn-1",
        org_id="org-1",
        owner_user_id="user-1",
        display_name="Codex",
        agent_kind="codex",
        transport="hosted_mcp",
        status="disabled",
        endpoint_url=None,
        remote_agent_id=None,
        remote_session_key=None,
        remote_agent_card={},
        capabilities={},
        last_seen_at=None,
        last_tested_at=None,
        last_error=None,
        metadata_={},
        disabled_at=datetime.now(timezone.utc),
        created_at=None,
        updated_at=None,
    )

    with patch(
        "brain.app.api.routers.agent_connections.external_agents.require_connection_for_user",
        return_value=connection,
    ) as require_connection, patch(
        "brain.app.api.routers.agent_connections.external_agents.disable_connection",
        return_value=connection,
    ) as disable_connection:
        response = await _request(
            "DELETE",
            "/api/agent-connections/conn-1",
            session=session,
        )

    assert response.status_code == 200
    assert response.json()["status"] == "disabled"
    require_connection.assert_awaited_once_with(
        session,
        connection_id="conn-1",
        org_id="org-1",
        user_id="user-1",
        role="member",
        require_manage=True,
    )
    disable_connection.assert_awaited_once_with(
        session,
        connection_id="conn-1",
        org_id="org-1",
    )


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
    assert names == {"illo_submit", "illo_read", "illo_act", "illo_get_result"}


async def test_hosted_mcp_invalid_token_returns_json_rpc_error():
    with patch(
        "brain.app.api.routers.agent_mcp.external_agents.authenticate_bridge_token",
        side_effect=external_agents.ExternalAgentAuthError("Invalid bridge token"),
    ):
        response = await _request(
            "POST",
            "/api/mcp",
            headers={"Authorization": "Bearer revoked-token"},
            json={"jsonrpc": "2.0", "id": 7, "method": "tools/list"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 7
    assert body["error"]["code"] == -32001
    assert body["error"]["message"] == "MCP authentication failed: Invalid bridge token"
    assert body["error"]["data"] == {"http_status": 401, "auth": "bearer"}


async def test_hosted_mcp_malformed_json_returns_invalid_request_without_auth():
    with patch(
        "brain.app.api.routers.agent_mcp.external_agents.authenticate_bridge_token",
    ) as authenticate:
        response = await _request(
            "POST",
            "/api/mcp",
            headers={
                "Authorization": "Bearer bridge-token",
                "Content-Type": "application/json",
            },
            content='{"jsonrpc":"2.0","id":7}{"extra":true}',
        )

    assert response.status_code == 400
    body = response.json()
    assert body == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32600, "message": "Invalid Request"},
    }
    authenticate.assert_not_called()


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
    assert names == {"illo_read"}


async def test_hosted_mcp_read_thread_get_accepts_canonical_thread_url():
    session = _AsyncSession()
    thread_id = "77777777-7777-4777-8777-777777777777"
    captured: dict[str, object] = {}

    async def fake_get_thread(db, principal, *, idea_id, limit):
        captured["db"] = db
        captured["principal"] = principal
        captured["idea_id"] = idea_id
        captured["limit"] = limit
        return {
            "idea": {"id": idea_id, "thread_id": idea_id},
            "thread_reference": {
                "type": "thread_reference",
                "object_type": "thread",
                "object_id": idea_id,
                "thread_id": idea_id,
                "thread_route": f"/threads/{idea_id}",
                "thread_url": f"https://illo.example.com/threads/{idea_id}",
                "status": "available",
                "title": "Shared thread",
            },
            "messages": [],
        }

    with patch(
        "brain.app.api.routers.agent_mcp.external_agents.authenticate_bridge_token",
        return_value=_principal(),
    ), patch(
        "brain.app.api.routers.agent_mcp.external_agents.get_thread",
        new=AsyncMock(side_effect=fake_get_thread),
    ):
        response = await _request(
            "POST",
            "/mcp",
            session=session,
            headers={"Authorization": "Bearer bridge-token"},
            json={
                "jsonrpc": "2.0",
                "id": 12,
                "method": "tools/call",
                "params": {
                    "name": "illo_read",
                    "arguments": {
                        "capability": "thread.get",
                        "arguments": {
                            "thread_url": f"https://illo.example.com/threads/{thread_id}",
                            "limit": 7,
                        },
                    },
                },
            },
        )

    assert response.status_code == 200
    payload = json.loads(response.json()["result"]["content"][0]["text"])
    assert captured["idea_id"] == thread_id
    assert captured["limit"] == 7
    assert payload["thread_reference"]["thread_route"] == f"/threads/{thread_id}"


async def test_launch_handoff_redirects_to_codex_deep_link():
    row = _handoff_row()

    with patch(
        "brain.app.api.routers.launch_handoffs.launch_handoffs.require_launch_handoff",
        new=AsyncMock(return_value=row),
    ) as require_handoff, patch(
        "brain.app.api.routers.launch_handoffs.launch_handoffs.mark_launch_handoff_launched",
        new=AsyncMock(return_value=row),
    ) as mark_launched, patch(
        "brain.app.api.routers.launch_handoffs.launch_handoffs.codex_deep_link_for_handoff",
        return_value="codex://threads/new?prompt=handoff",
    ):
        response = await _request(
            "GET",
            f"/codex/handoffs/{row.id}",
            follow_redirects=False,
        )

    assert response.status_code == 302
    assert response.headers["location"] == "codex://threads/new?prompt=handoff"
    require_handoff.assert_awaited_once()
    assert require_handoff.await_args.kwargs["org_id"] == "org-1"
    mark_launched.assert_awaited_once()
    assert mark_launched.await_args.kwargs["launched_by_user_id"] == "user-1"


async def test_launch_handoff_redirect_requires_org_context():
    overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = lambda: _AsyncSession()
    app.dependency_overrides[get_current_user] = lambda: {"id": "user-1", "role": "member"}
    app.dependency_overrides[rate_limit] = lambda: None
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get(
                "/codex/handoffs/88888888-8888-4888-8888-888888888888",
                follow_redirects=False,
            )
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(overrides)

    assert response.status_code == 403


async def test_launch_handoff_api_create_returns_launch_url():
    row = _handoff_row()

    with patch(
        "brain.app.api.routers.launch_handoffs.launch_handoffs.create_launch_handoff",
        new=AsyncMock(return_value=row),
    ) as create:
        response = await _request(
            "POST",
            "/api/launch-handoffs",
            json={
                "title": "Launch in Codex",
                "instructions": "Fetch context and implement.",
                "source_surface": "webapp",
                "repo_origin_url": "git@github.com:uwear-ai/illospace-project.git",
            },
        )

    assert response.status_code == 201
    payload = response.json()["handoff"]
    assert payload["id"] == row.id
    assert payload["launch_url"].endswith(f"/codex/handoffs/{row.id}")
    create.assert_awaited_once()


async def test_hosted_mcp_submit_builds_submission_envelope():
    session = _AsyncSession()
    captured: dict[str, object] = {}

    async def fake_submit(db, *, connection, envelope, ingress_context):
        captured["db"] = db
        captured["connection"] = connection
        captured["envelope"] = envelope
        captured["ingress_context"] = ingress_context
        return {
            "status": "review_required",
            "event_id": "evt-1",
            "confidence": None,
            "ilo_outcome": {
                "operation": "queued",
                "message": "Illo handling queued.",
                "handling": {
                    "status": "queued",
                    "event_id": "evt-1",
                    "run_id": "run-1",
                },
            },
        }

    with patch(
        "brain.app.api.routers.agent_mcp.external_agents.authenticate_bridge_token",
        return_value=_principal(),
    ), patch(
        "brain.app.api.routers.agent_mcp.submit_inbound_envelope",
        new=AsyncMock(side_effect=fake_submit),
    ) as submit:
        response = await _request(
            "POST",
            "/mcp",
            session=session,
            headers={"Authorization": "Bearer bridge-token"},
            json={
                "jsonrpc": "2.0",
                "id": 11,
                "method": "tools/call",
                "params": {
                    "name": "illo_submit",
                    "arguments": {
                        "message": "Ask Illo to review the implementation context and decide next steps.",
                        "origin": "codex.submit",
                        "source_tool": "codex",
                        "repo": "illospace-project",
                        "branch": "codex/mcp-submit",
                        "task_title": "MCP submit lane",
                        "files_touched": ["brain/app/api/routers/agent_mcp.py"],
                        "parts": [{"type": "text", "text": "Implemented the submit tool."}],
                        "response": {"mode": "webhook"},
                        "idempotency_key": "codex:run-1",
                        "metadata": {"hook": "post-message"},
                    },
                },
            },
        )

    assert response.status_code == 200
    payload = json.loads(response.json()["result"]["content"][0]["text"])
    assert payload["status"] == "review_required"
    assert payload["event_id"] == "evt-1"
    assert payload["submission_id"] == "evt-1"
    assert payload["result_id"] == "evt-1"
    assert payload["operation"] == "queued"
    assert payload["run_id"] == "run-1"
    assert payload["handling_status"] == "queued"
    submit.assert_awaited_once()
    assert captured["db"] is session
    assert captured["connection"] == {
        "id": "conn-1",
        "org_id": "org-1",
        "owner_user_id": "user-1",
        "token_id": "token-1",
        "display_name": "Hermes",
        "agent_kind": "hermes",
        "source_type": "personal_tool",
        "capabilities": ["illo_submit", "illo_read", "illo_act", "illo_get_result"],
    }
    assert captured["envelope"] == {
        "kind": "submission",
        "origin": "codex.submit",
        "payload": {
            "message": "Ask Illo to review the implementation context and decide next steps.",
            "parts": [{"type": "text", "text": "Implemented the submit tool."}],
            "source": {
                "source_tool": "codex",
                "repo": "illospace-project",
                "branch": "codex/mcp-submit",
                "task_title": "MCP submit lane",
                "files_touched": ["brain/app/api/routers/agent_mcp.py"],
            },
            "constraints": {},
            "correlation": {},
            "response": {"mode": "webhook"},
        },
        "summary": "Ask Illo to review the implementation context and decide next steps.",
        "message": "Ask Illo to review the implementation context and decide next steps.",
        "parts": [{"type": "text", "text": "Implemented the submit tool."}],
        "source": {
            "source_tool": "codex",
            "repo": "illospace-project",
            "branch": "codex/mcp-submit",
            "task_title": "MCP submit lane",
            "files_touched": ["brain/app/api/routers/agent_mcp.py"],
        },
        "constraints": {},
        "correlation": {},
        "response": {"mode": "webhook"},
        "idempotency_key": "codex:run-1",
    }
    ingress_context = captured["ingress_context"]
    assert ingress_context["surface"] == "mcp_personal_tool"
    assert ingress_context["source_actor"]["connection_id"] == "conn-1"
    assert ingress_context["authority_principal"] == {
        "kind": "user",
        "user_id": "user-1",
        "org_id": "org-1",
    }
    assert ingress_context["metadata"]["hook"] == "post-message"
    assert ingress_context["metadata"]["mcp_tool"] == "illo_submit"


async def test_hosted_mcp_submit_commits_before_later_batch_rollback():
    order: list[str] = []
    session = _AsyncSession(order)

    async def fake_submit(_db, *, connection, envelope, ingress_context):
        order.append("context-write")
        return {"status": "processed", "event_id": "evt-1", "connection_id": connection["id"]}

    with patch(
        "brain.app.api.routers.agent_mcp.external_agents.authenticate_bridge_token",
        return_value=_principal(),
    ), patch(
        "brain.app.api.routers.agent_mcp.submit_inbound_envelope",
        new=AsyncMock(side_effect=fake_submit),
    ), patch(
        "brain.app.api.routers.agent_mcp.external_agents.get_thread",
        side_effect=RuntimeError("thread lookup exploded"),
    ):
        response = await _request(
            "POST",
            "/mcp",
            session=session,
            headers={"Authorization": "Bearer bridge-token"},
            json=[
                {
                    "jsonrpc": "2.0",
                    "id": 21,
                    "method": "tools/call",
                    "params": {
                        "name": "illo_submit",
                        "arguments": {"message": "Submission should be durable before batch failure."},
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "id": 22,
                    "method": "tools/call",
                    "params": {
                        "name": "illo_read",
                        "arguments": {
                            "capability": "thread.get",
                            "arguments": {"idea_id": "idea-missing"},
                        },
                    },
                },
            ],
        )

    assert response.status_code == 200
    first, second = response.json()
    context_result = json.loads(first["result"]["content"][0]["text"])
    assert context_result["event_id"] == "evt-1"
    assert second["result"]["isError"] is True
    assert order == ["context-write", "commit", "rollback"]


async def test_hosted_mcp_submit_requires_signal_scope():
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
    ), patch(
        "brain.app.api.routers.agent_mcp.submit_inbound_envelope",
        new=AsyncMock(),
    ) as submit:
        response = await _request(
            "POST",
            "/mcp",
            headers={"Authorization": "Bearer bridge-token"},
            json={
                "jsonrpc": "2.0",
                "id": 12,
                "method": "tools/call",
                "params": {
                    "name": "illo_submit",
                    "arguments": {"message": "Share context with Illo."},
                },
            },
        )

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is True
    assert "signal:submit" in result["content"][0]["text"]
    submit.assert_not_called()


async def test_hosted_mcp_submit_rejects_direct_workspace_targets():
    with patch(
        "brain.app.api.routers.agent_mcp.external_agents.authenticate_bridge_token",
        return_value=_principal(),
    ), patch(
        "brain.app.api.routers.agent_mcp.submit_inbound_envelope",
        new=AsyncMock(),
    ) as submit:
        response = await _request(
            "POST",
            "/mcp",
            headers={"Authorization": "Bearer bridge-token"},
            json={
                "jsonrpc": "2.0",
                "id": 13,
                "method": "tools/call",
                "params": {
                    "name": "illo_submit",
                    "arguments": {
                        "message": "Please post this context.",
                        "idea_id": "idea-1",
                    },
                },
            },
        )

    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is True
    assert "direct workspace targets" in result["content"][0]["text"]
    submit.assert_not_called()


async def test_hosted_mcp_get_result_reads_inbound_event_and_receipts():
    event = SimpleNamespace(
        id="evt-1",
        org_id="org-1",
        connection_id="conn-1",
        status="review_required",
        action_result={"handling": {"status": "queued", "run_id": "run-1"}},
    )
    receipt = SimpleNamespace(id="receipt-1")

    with patch(
        "brain.app.api.routers.agent_mcp.external_agents.authenticate_bridge_token",
        return_value=_principal(),
    ), patch(
        "brain.app.api.routers.agent_mcp.inbound_admin.require_event_for_org",
        new=AsyncMock(return_value=event),
    ) as require_event, patch(
        "brain.app.api.routers.agent_mcp.inbound_admin.list_receipts",
        new=AsyncMock(return_value=[receipt]),
    ) as list_receipts, patch(
        "brain.app.api.routers.agent_mcp.inbound_admin.serialize_event",
        return_value={"id": "evt-1", "status": "review_required"},
    ), patch(
        "brain.app.api.routers.agent_mcp.inbound_admin.serialize_receipt",
        return_value={"id": "receipt-1", "status": "review_required"},
    ):
        response = await _request(
            "POST",
            "/mcp",
            headers={"Authorization": "Bearer bridge-token"},
            json={
                "jsonrpc": "2.0",
                "id": 14,
                "method": "tools/call",
                "params": {
                    "name": "illo_get_result",
                    "arguments": {"event_id": "evt-1"},
                },
            },
        )

    assert response.status_code == 200
    payload = json.loads(response.json()["result"]["content"][0]["text"])
    assert payload["event_id"] == "evt-1"
    assert payload["submission_id"] == "evt-1"
    assert payload["status"] == "review_required"
    assert payload["handling_status"] == "queued"
    assert payload["run_id"] == "run-1"
    assert payload["latest_receipt"] == {"id": "receipt-1", "status": "review_required"}
    require_event.assert_awaited_once()
    list_receipts.assert_awaited_once()


async def test_hosted_mcp_read_handoff_get_returns_context():
    row = _handoff_row()

    with patch(
        "brain.app.api.routers.agent_mcp.external_agents.authenticate_bridge_token",
        return_value=_principal(),
    ), patch(
        "brain.app.api.routers.agent_mcp_handoffs.launch_handoffs.require_launch_handoff",
        new=AsyncMock(return_value=row),
    ) as require_handoff:
        response = await _request(
            "POST",
            "/mcp",
            headers={"Authorization": "Bearer bridge-token"},
            json={
                "jsonrpc": "2.0",
                "id": 15,
                "method": "tools/call",
                "params": {
                    "name": "illo_read",
                    "arguments": {
                        "capability": "handoff.get",
                        "arguments": {"url": f"https://illo.example.com/codex/handoffs/{row.id}"},
                    },
                },
            },
        )

    assert response.status_code == 200
    payload = json.loads(response.json()["result"]["content"][0]["text"])
    assert payload["handoff"]["id"] == row.id
    assert payload["handoff"]["instructions"] == row.instructions
    require_handoff.assert_awaited_once()


async def test_hosted_mcp_create_handoff_commits_and_returns_launch_url():
    order: list[str] = []
    session = _AsyncSession(order)
    row = _handoff_row()

    with patch(
        "brain.app.api.routers.agent_mcp.external_agents.authenticate_bridge_token",
        return_value=_principal(),
    ), patch(
        "brain.app.api.routers.agent_mcp_handoffs.launch_handoffs.create_launch_handoff",
        new=AsyncMock(return_value=row),
    ) as create:
        response = await _request(
            "POST",
            "/mcp",
            session=session,
            headers={"Authorization": "Bearer bridge-token"},
            json={
                "jsonrpc": "2.0",
                "id": 16,
                "method": "tools/call",
                "params": {
                    "name": "illo_act",
                    "arguments": {
                        "capability": "handoff.create",
                        "arguments": {
                            "title": "Launch in Codex",
                            "instructions": "Fetch context and implement.",
                            "repo_origin_url": "git@github.com:uwear-ai/illospace-project.git",
                            "source_surface": "slack",
                            "source_ref": {"channel_id": "C1"},
                        },
                    },
                },
            },
        )

    assert response.status_code == 200
    payload = json.loads(response.json()["result"]["content"][0]["text"])
    assert payload["handoff"]["id"] == row.id
    assert payload["launch_url"].endswith(f"/codex/handoffs/{row.id}")
    assert order == ["commit"]
    create.assert_awaited_once()


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
                    "name": "illo_act",
                    "arguments": {
                        "capability": "thread.create",
                        "arguments": {"title": "Shared work", "body": "Shared from Hermes"},
                    },
                },
            },
        )

    assert response.status_code == 200
    payload = response.json()
    content = payload["result"]["content"][0]["text"]
    assert json.loads(content)["idea"]["id"] == "idea-1"
    assert order[:2] == ["commit", "broadcast:thread_message"]


async def test_hosted_mcp_create_thread_routes_trigger_when_requested():
    order: list[str] = []
    session = _AsyncSession(order)
    idea = SimpleNamespace(
        id="idea-1",
        title="Coordinate setup",
        description=None,
        attachments=[],
        status="active",
        org_id="org-1",
        origin="external_agent",
        origin_ref=None,
    )
    message = SimpleNamespace(
        id=10,
        idea_id="idea-1",
        role="user",
        content="Please coordinate with JB and Axel",
        attachments=[],
        metadata_={},
        user_id="user-1",
        message_type="trigger",
        created_at=datetime.now(timezone.utc),
    )
    route_result = MagicMock()
    route_result.to_response.return_value = {"ok": True, "route": "run", "run_id": 123}

    async def broadcast(event_type, payload, **_kwargs):
        order.append(f"broadcast:{event_type}")

    with patch(
        "brain.app.api.routers.agent_mcp.external_agents.authenticate_bridge_token",
        return_value=_principal(),
    ), patch(
        "brain.app.api.routers.agent_mcp.external_agents.create_thread_from_agent",
        return_value=(idea, message, []),
    ), patch(
        "brain.app.triggers.router.async_route_trigger",
        new=AsyncMock(return_value=route_result),
    ) as route_trigger, patch(
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
                "id": 8,
                "method": "tools/call",
                "params": {
                    "name": "illo_act",
                    "arguments": {
                        "capability": "thread.create",
                        "arguments": {
                            "title": "Coordinate setup",
                            "body": "Please coordinate with JB and Axel",
                            "trigger_illo": True,
                        },
                    },
                },
            },
        )

    assert response.status_code == 200
    result = json.loads(response.json()["result"]["content"][0]["text"])
    assert result["trigger"] == {"ok": True, "route": "run", "run_id": 123}
    route_trigger.assert_awaited_once()
    trigger = route_trigger.call_args.args[0]
    assert trigger.event_type == "cortex.thread_reply"
    assert trigger.target["idea_id"] == "idea-1"
    assert "Please coordinate with JB and Axel" in trigger.payload["run_message"]
    request_source = trigger.payload["metadata"]["request_source"]
    assert request_source["surface"] == "mcp_personal_agent"
    assert request_source["personal_agent"] == "Hermes"
    assert request_source["visibility"] == "visible_team_thread"
    assert order[:2] == ["commit", "broadcast:thread_message"]


async def test_hosted_mcp_create_thread_rolls_back_when_trigger_fails():
    order: list[str] = []
    session = _AsyncSession(order)
    idea = SimpleNamespace(
        id="idea-1",
        title="Dead message",
        description=None,
        attachments=[],
        status="active",
        org_id="org-1",
        origin="external_agent",
        origin_ref=None,
    )
    message = SimpleNamespace(
        id=10,
        idea_id="idea-1",
        role="user",
        content="This should not persist without a trigger",
        attachments=[],
        metadata_={},
        user_id="user-1",
        message_type="trigger",
        created_at=datetime.now(timezone.utc),
    )

    with patch(
        "brain.app.api.routers.agent_mcp.external_agents.authenticate_bridge_token",
        return_value=_principal(),
    ), patch(
        "brain.app.api.routers.agent_mcp.external_agents.create_thread_from_agent",
        return_value=(idea, message, []),
    ), patch(
        "brain.app.api.routers.agent_mcp._run_trigger_if_requested",
        side_effect=ImportError("cannot import name 'route_trigger'"),
    ), patch(
        "brain.app.api.routers.agent_bridge.ws_manager.broadcast_product_event",
        new=AsyncMock(),
    ) as broadcast:
        response = await _request(
            "POST",
            "/mcp",
            session=session,
            headers={"Authorization": "Bearer bridge-token"},
            json={
                "jsonrpc": "2.0",
                "id": 9,
                "method": "tools/call",
                "params": {
                    "name": "illo_act",
                    "arguments": {
                        "capability": "thread.create",
                        "arguments": {
                            "title": "Dead message",
                            "body": "This should not persist without a trigger",
                            "trigger_illo": True,
                        },
                    },
                },
            },
        )

    assert response.status_code == 200
    assert response.json()["result"]["isError"] is True
    assert order == ["rollback"]
    broadcast.assert_not_called()
