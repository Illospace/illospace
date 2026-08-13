from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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


def _skill_row(**overrides):
    data = {
        "id": 17,
        "name": "maintainer-loop",
        "description": "Maintain assigned work.",
        "procedure": "Read the pinned ticket and complete its acceptance criteria.",
        "version": 4,
        "level": "cognitive",
        "skill_type": "skill",
        "maturity": "stable",
        "confidence": 0.9,
        "use_count": 12,
        "success_count": 10,
        "failure_count": 1,
        "partial_count": 1,
        "avg_duration_sec": 42.5,
        "last_used": None,
        "pitfalls": ["Using a stale mirror"],
        "refinements": ["Pin the skill id"],
        "triggers": [{"direction": "for", "pattern": "assigned work"}],
        "guardrails": [{"text": "Do not push", "severity": "warning"}],
        "graduated_steps": [{"step": "verify"}],
        "auto_emerged": False,
        "builtin": False,
        "archived": False,
        "thinking_tier": "medium",
        "skill_installation_id": 27,
        "bundle_version_id": 37,
        "bundle_digest": "sha256:bundle",
        "overlay_revision": 3,
        "effective_digest": "sha256:effective",
        "source_kind": "private_local",
        "trust_level": "private_local",
    }
    data.update(overrides)
    return SimpleNamespace(**data)


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


async def test_hosted_mcp_skills_get_reads_same_stored_skill_by_id_and_name():
    session = _AsyncSession()
    skill = _skill_row()
    calls: list[dict[str, object]] = []

    class FakeSkillRepository:
        def __init__(self, db):
            assert db is session

        async def a_get_visible(self, **kwargs):
            calls.append(kwargs)
            if kwargs.get("skill_id") == skill.id or kwargs.get("name") == skill.name:
                return skill
            return None

    with patch(
        "brain.app.api.routers.agent_mcp.external_agents.authenticate_bridge_token",
        return_value=_principal(),
    ), patch(
        "brain.app.api.routers.agent_mcp_skills.SkillRepository",
        FakeSkillRepository,
    ):
        responses = []
        for arguments in ({"skill_id": skill.id}, {"name": skill.name}):
            responses.append(
                await _request(
                    "POST",
                    "/api/mcp",
                    session=session,
                    headers={"Authorization": "Bearer bridge-token"},
                    json={
                        "jsonrpc": "2.0",
                        "id": 33,
                        "method": "tools/call",
                        "params": {
                            "name": "illo_read",
                            "arguments": {
                                "capability": "skills.get",
                                "arguments": arguments,
                            },
                        },
                    },
                )
            )

    payloads = [json.loads(response.json()["result"]["content"][0]["text"]) for response in responses]
    assert payloads[0] == payloads[1]
    assert payloads[0]["skill"]["procedure"] == skill.procedure
    assert payloads[0]["skill"]["archived"] is False
    assert payloads[0]["skill"]["graduated_steps"] == [{"step": "verify"}]
    assert payloads[0]["skill"]["overlay_revision"] == 3
    assert calls == [
        {"org_id": "org-1", "user_id": "user-1", "skill_id": 17, "name": None},
        {"org_id": "org-1", "user_id": "user-1", "skill_id": None, "name": skill.name},
    ]


@pytest.mark.parametrize("arguments", [{"skill_id": 404}, {"name": "missing"}, {"name": "retired"}])
async def test_hosted_mcp_skills_get_returns_not_found_for_unknown_or_archived(arguments):
    class FakeSkillRepository:
        def __init__(self, db):
            self.db = db

        async def a_get_visible(self, **kwargs):
            return None

    with patch(
        "brain.app.api.routers.agent_mcp.external_agents.authenticate_bridge_token",
        return_value=_principal(),
    ), patch(
        "brain.app.api.routers.agent_mcp_skills.SkillRepository",
        FakeSkillRepository,
    ):
        response = await _request(
            "POST",
            "/api/mcp",
            headers={"Authorization": "Bearer bridge-token"},
            json={
                "jsonrpc": "2.0",
                "id": 34,
                "method": "tools/call",
                "params": {
                    "name": "illo_read",
                    "arguments": {
                        "capability": "skills.get",
                        "arguments": arguments,
                    },
                },
            },
        )

    assert response.status_code == 200
    assert response.json()["result"] == {
        "content": [{"type": "text", "text": "Skill not found"}],
        "isError": True,
    }


async def test_hosted_mcp_skills_list_is_scoped_and_omits_full_procedures():
    session = _AsyncSession()
    skill = _skill_row()
    calls: list[dict[str, str]] = []

    class FakeSkillRepository:
        def __init__(self, db):
            assert db is session

        async def a_list_visible(self, **kwargs):
            calls.append(kwargs)
            return [skill]

    with patch(
        "brain.app.api.routers.agent_mcp.external_agents.authenticate_bridge_token",
        return_value=_principal(),
    ), patch(
        "brain.app.api.routers.agent_mcp_skills.SkillRepository",
        FakeSkillRepository,
    ):
        response = await _request(
            "POST",
            "/api/mcp",
            session=session,
            headers={"Authorization": "Bearer bridge-token"},
            json={
                "jsonrpc": "2.0",
                "id": 35,
                "method": "tools/call",
                "params": {
                    "name": "illo_read",
                    "arguments": {"capability": "skills.list"},
                },
            },
        )

    payload = json.loads(response.json()["result"]["content"][0]["text"])
    assert payload == {
        "skills": [{"id": 17, "name": "maintainer-loop", "version": 4, "archived": False}]
    }
    assert "procedure" not in payload["skills"][0]
    assert calls == [{"org_id": "org-1", "user_id": "user-1"}]


async def test_hosted_mcp_capabilities_describe_skill_reads():
    with patch(
        "brain.app.api.routers.agent_mcp.external_agents.authenticate_bridge_token",
        return_value=_principal(),
    ):
        response = await _request(
            "POST",
            "/api/mcp",
            headers={"Authorization": "Bearer bridge-token"},
            json={
                "jsonrpc": "2.0",
                "id": 36,
                "method": "tools/call",
                "params": {
                    "name": "illo_read",
                    "arguments": {"capability": "capabilities"},
                },
            },
        )

    payload = json.loads(response.json()["result"]["content"][0]["text"])
    capabilities = {item["name"]: item for item in payload["capabilities"]}
    assert capabilities["skills.get"]["arguments"] == {
        "skill_id": "integer",
        "name": "string",
    }
    assert capabilities["skills.list"]["arguments"] == {}
