from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from brain.app.api.auth import get_current_user
from brain.app.api.deps import get_db, rate_limit
from brain.app.api.main import app
from brain.systems.external_agents import service as external_agents


pytestmark = pytest.mark.asyncio


class _AsyncSession:
    def __init__(self):
        self.sync_session = MagicMock()

    async def run_sync(self, fn):
        return fn(self.sync_session)

    async def commit(self):
        return None

    async def rollback(self):
        return None

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


async def _request(method: str, path: str, *, session: _AsyncSession | None = None, **kwargs):
    overrides = dict(app.dependency_overrides)
    app.dependency_overrides[get_db] = lambda: session or _AsyncSession()
    app.dependency_overrides[get_current_user] = lambda: {
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


async def test_hosted_mcp_read_capabilities_include_project_context_search():
    with patch(
        "brain.app.api.routers.agent_mcp.external_agents.authenticate_bridge_token",
        return_value=_principal(),
    ):
        response = await _request(
            "POST",
            "/mcp",
            headers={"Authorization": "Bearer bridge-token"},
            json={
                "jsonrpc": "2.0",
                "id": 13,
                "method": "tools/call",
                "params": {
                    "name": "illo_read",
                    "arguments": {"capability": "capabilities"},
                },
            },
        )

    assert response.status_code == 200
    payload = json.loads(response.json()["result"]["content"][0]["text"])
    names = {capability["name"] for capability in payload["capabilities"]}
    assert "project_contexts.search" in names
    details = next(capability for capability in payload["capabilities"] if capability["name"] == "project_contexts.search")
    assert "Project Context" in details["description"]


async def test_hosted_mcp_read_project_contexts_search_uses_bridge_principal_scope():
    session = _AsyncSession()
    captured: dict[str, object] = {}

    async def fake_search_project_contexts(db, *, org_id, user_id, query, limit, include_inactive):
        captured["db"] = db
        captured["org_id"] = org_id
        captured["user_id"] = user_id
        captured["query"] = query
        captured["limit"] = limit
        captured["include_inactive"] = include_inactive
        return {
            "query": query,
            "results": [
                {
                    "type": "project_context_profile",
                    "slug": "aritzia-uwear-client-project",
                    "name": "Aritzia / Uwear Client Project",
                    "resources": {"count": 2, "items": []},
                }
            ],
        }

    with patch(
        "brain.app.api.routers.agent_mcp.external_agents.authenticate_bridge_token",
        return_value=_principal(),
    ), patch(
        "brain.app.api.routers.agent_mcp.search_project_contexts",
        new=AsyncMock(side_effect=fake_search_project_contexts),
    ):
        response = await _request(
            "POST",
            "/mcp",
            session=session,
            headers={"Authorization": "Bearer bridge-token"},
            json={
                "jsonrpc": "2.0",
                "id": 14,
                "method": "tools/call",
                "params": {
                    "name": "illo_read",
                    "arguments": {
                        "capability": "project_contexts.search",
                        "arguments": {
                            "query": "Aritzia 85K asset pilot",
                            "limit": 7,
                            "include_inactive": True,
                        },
                    },
                },
            },
        )

    assert response.status_code == 200
    payload = json.loads(response.json()["result"]["content"][0]["text"])
    assert payload["results"][0]["slug"] == "aritzia-uwear-client-project"
    assert captured == {
        "db": session,
        "org_id": "org-1",
        "user_id": "user-1",
        "query": "Aritzia 85K asset pilot",
        "limit": 7,
        "include_inactive": True,
    }
