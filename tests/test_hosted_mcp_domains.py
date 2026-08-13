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


def _skill_row(**overrides):
    data = {
        "id": 17,
        "name": "maintainer-loop",
        "description": "Maintain assigned work.",
        "procedure": "Read the pinned ticket and complete its acceptance criteria.",
        "triggers": [{"direction": "for", "pattern": "assigned work"}],
        "guardrails": [{"text": "Do not push", "severity": "warning"}],
        "pitfalls": ["Using a stale mirror"],
        "refinements": ["Pin the skill id"],
        "version": 4,
        "archived": False,
        "level": "cognitive",
        "skill_type": "skill",
        "maturity": "stable",
        "thinking_tier": "medium",
        "builtin": False,
        "skill_installation_id": 27,
        "bundle_version_id": 37,
        "bundle_digest": "sha256:bundle",
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


async def test_hosted_mcp_inspect_domains_reads_schema_and_optional_records():
    session = _AsyncSession()
    domain = SimpleNamespace(id=3, slug="crm", name="CRM")
    record = SimpleNamespace(id=9, title="Acme", data={"status": "active", "body": "x" * 500})
    relation = SimpleNamespace(id=4)
    event = SimpleNamespace(id=7, event_type="record.created")

    class FakeDomainService:
        def __init__(self, db):
            self.db = db

        async def list_domains(self, org_id, *, include_archived=False):
            assert org_id == "org-1"
            assert include_archived is False
            return [domain]

        async def serialize_domain_summary(self, item):
            return {"id": item.id, "slug": item.slug, "name": item.name}

        async def get_domain(self, org_id, domain_id, *, include_archived=False):
            assert org_id == "org-1"
            assert domain_id == 3
            assert include_archived is False
            return domain

        async def serialize_domain_schema(self, item):
            return {"id": item.id, "slug": item.slug, "objects": [{"key": "company"}]}

        async def list_records(
            self,
            org_id,
            domain_id,
            *,
            object_key=None,
            search=None,
            filters=None,
            include_archived=False,
            limit=100,
            order="updated_desc",
            offset=0,
        ):
            assert (org_id, domain_id, object_key, search, filters, include_archived, limit, order, offset) == (
                "org-1",
                3,
                "company",
                "acme",
                {"assignee": "Reda"},
                False,
                2,
                "updated_asc",
                0,
            )
            return [record]

        async def serialize_record(self, item):
            return {"id": item.id, "title": item.title}

        async def serialize_record_compact(self, item, *, fields=None):
            assert fields == ["status"]
            return {"id": item.id, "title": item.title, "data": {"status": item.data["status"]}}

        async def count_records(
            self,
            org_id,
            domain_id,
            *,
            object_key=None,
            search=None,
            include_archived=False,
            filters=None,
        ):
            assert (org_id, domain_id, object_key, search, include_archived, filters) == (
                "org-1",
                3,
                "company",
                "acme",
                False,
                {"assignee": "Reda"},
            )
            return 5

        async def list_relations(
            self,
            org_id,
            domain_id,
            *,
            relation_key=None,
            source_record_id=None,
            target_record_id=None,
            include_archived=False,
            limit=100,
        ):
            assert (org_id, domain_id, relation_key, source_record_id, target_record_id, limit) == (
                "org-1",
                3,
                None,
                None,
                None,
                2,
            )
            return [relation]

        async def serialize_relation(self, item):
            return {"id": item.id}

        async def list_events(self, org_id, domain_id, *, record_id=None, limit=50, offset=0):
            assert (org_id, domain_id, record_id, limit, offset) == ("org-1", 3, None, 2, 0)
            return [event]

        async def count_events(self, org_id, domain_id, *, record_id=None):
            assert (org_id, domain_id, record_id) == ("org-1", 3, None)
            return 1

        def serialize_event(self, item):
            return {"id": item.id, "event_type": item.event_type}

    with patch(
        "brain.app.api.routers.agent_mcp.external_agents.authenticate_bridge_token",
        return_value=_principal(),
    ), patch("brain.app.api.routers.agent_mcp_domains.AsyncDomainService", FakeDomainService):
        response = await _request(
            "POST",
            "/api/mcp",
            session=session,
            headers={"Authorization": "Bearer bridge-token"},
            json={
                "jsonrpc": "2.0",
                "id": 31,
                "method": "tools/call",
                "params": {
                    "name": "illo_read",
                    "arguments": {
                        "capability": "domain.inspect",
                        "arguments": {
                            "domain_id": 3,
                            "object_key": "company",
                            "search": "acme",
                            "filters": {"assignee": "Reda"},
                            "format": "compact",
                            "fields": ["status"],
                            "order": "updated_asc",
                            "include_records": True,
                            "include_relations": True,
                            "include_events": True,
                            "limit": 2,
                        },
                    },
                },
            },
        )

    assert response.status_code == 200
    payload = json.loads(response.json()["result"]["content"][0]["text"])
    next_page = payload.pop("next_page")
    assert next_page
    assert payload == {
        "domain": {"id": 3, "slug": "crm", "objects": [{"key": "company"}]},
        "records": [{"id": 9, "title": "Acme", "data": {"status": "active"}}],
        "returned": 1,
        "total_matching": 5,
        "order": "updated_asc",
        "format": "compact",
        "relations": [{"id": 4}],
        "events": [{"id": 7, "event_type": "record.created"}],
        "event_total_matching": 1,
        "truncated": True,
        "evidence_health": {"status": "ok", "completeness": "more_available"},
    }
    assert session.order == []


async def test_hosted_mcp_write_domain_record_creates_record_with_trace_and_commits():
    session = _AsyncSession()
    domain = SimpleNamespace(id=3, slug="crm")
    record = SimpleNamespace(id=9, title="Acme")
    captured: dict[str, object] = {}

    class FakeDomainService:
        def __init__(self, db):
            self.db = db

        async def get_domain(self, org_id, domain_id, *, include_archived=False):
            captured["get_domain"] = (org_id, domain_id, include_archived)
            return domain

        async def create_record(
            self,
            org_id,
            domain_id,
            object_key,
            *,
            data,
            title=None,
            actor_id=None,
            actor_kind="human",
            run_id=None,
            idea_id=None,
            reason=None,
        ):
            captured["create_record"] = {
                "org_id": org_id,
                "domain_id": domain_id,
                "object_key": object_key,
                "data": data,
                "title": title,
                "actor_id": actor_id,
                "actor_kind": actor_kind,
                "run_id": run_id,
                "idea_id": idea_id,
                "reason": reason,
            }
            return record

        async def serialize_record(self, item):
            return {"id": item.id, "title": item.title}

    with patch(
        "brain.app.api.routers.agent_mcp.external_agents.authenticate_bridge_token",
        return_value=_principal(),
    ), patch("brain.app.api.routers.agent_mcp_domains.AsyncDomainService", FakeDomainService):
        response = await _request(
            "POST",
            "/api/mcp",
            session=session,
            headers={"Authorization": "Bearer bridge-token"},
            json={
                "jsonrpc": "2.0",
                "id": 32,
                "method": "tools/call",
                "params": {
                    "name": "illo_act",
                    "arguments": {
                        "capability": "domain.record.write",
                        "arguments": {
                            "action": "create_record",
                            "domain_id": 3,
                            "object_key": "company",
                            "data": {"name": "Acme"},
                            "title": "Acme",
                            "reason": "User asked personal agent to add a company",
                        },
                    },
                },
            },
        )

    assert response.status_code == 200
    payload = json.loads(response.json()["result"]["content"][0]["text"])
    assert payload == {"record": {"id": 9, "title": "Acme"}}
    assert captured["get_domain"] == ("org-1", 3, False)
    assert captured["create_record"] == {
        "org_id": "org-1",
        "domain_id": 3,
        "object_key": "company",
        "data": {"name": "Acme"},
        "title": "Acme",
        "actor_id": "user-1",
        "actor_kind": "personal_agent",
        "run_id": None,
        "idea_id": None,
        "reason": (
            "User asked personal agent to add a company | illo_write_domain_record via Hermes "
            "(hermes); connection_id=conn-1; token_id=token-1"
        ),
    }
    assert session.order == ["commit"]


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
    ), patch("brain.app.api.routers.agent_mcp.SkillRepository", FakeSkillRepository):
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
    ), patch("brain.app.api.routers.agent_mcp.SkillRepository", FakeSkillRepository):
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
    ), patch("brain.app.api.routers.agent_mcp.SkillRepository", FakeSkillRepository):
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
