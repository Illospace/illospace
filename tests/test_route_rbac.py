"""Focused RBAC coverage for privileged non-Cortex API routes."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from brain.app.api.auth import get_current_user
from brain.app.api.authorization import PERMISSION_SCHEDULER_MANAGE
from brain.app.api.deps import get_db


MEMBER = {
    "id": "user-1",
    "org_id": "org-1",
    "role": "member",
    "permissions": [],
}


@pytest_asyncio.fixture
async def client():
    from brain.app.api.main import app

    app.dependency_overrides.clear()
    app.dependency_overrides[get_db] = lambda: MagicMock()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, app
    app.dependency_overrides.clear()


def _act_as(app, user: dict) -> None:
    app.dependency_overrides[get_current_user] = lambda: user


def _skill_obj(**overrides):
    fields = {
        "id": 1,
        "name": "demo",
        "description": "Demo skill",
        "procedure": "Do the thing",
        "version": 1,
        "maturity": "emerging",
        "confidence": 0.3,
        "use_count": 0,
        "success_count": 0,
        "failure_count": 0,
        "partial_count": 0,
        "avg_duration_sec": None,
        "last_used": None,
        "pitfalls": [],
        "refinements": [],
        "triggers": [],
        "guardrails": [],
        "auto_emerged": False,
        "provider": None,
        "model_name": None,
        "reasoning_effort": None,
        "service_tier": None,
        "auth_mode": None,
        "model_tier": "medium",
        "thinking_tier": "medium",
        "success_rate": 0.0,
        "children": [],
        "executions": [],
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "url", "body"),
    [
        ("POST", "/api/system/scheduler/sync", {}),
        ("POST", "/api/system/scheduler/materialize", {}),
        ("POST", "/api/system/scheduler/drain", {}),
        ("POST", "/api/system/scheduler/jobs", {"job_key": "demo", "cron_expr": "0 8 * * *", "handler_ref": "python -m demo"}),
        ("DELETE", "/api/system/scheduler/jobs/demo", None),
        ("POST", "/api/system/scheduler/jobs/demo/pause", {}),
        ("POST", "/api/system/scheduler/jobs/demo/resume", None),
        ("POST", "/api/system/scheduler/jobs/demo/owner-mode", {"owner_mode": "scheduler"}),
        ("POST", "/api/system/scheduler/jobs/demo/load-shed", {"max_concurrency": 1}),
        ("POST", "/api/system/scheduler/runs/1/resume", None),
        ("POST", "/api/system/scheduler/runs/1/retry", None),
    ],
)
async def test_member_cannot_mutate_scheduler(client, method, url, body):
    c, app = client
    _act_as(app, MEMBER)

    response = await c.request(method, url, json=body)

    assert response.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "url", "body"),
    [
        ("GET", "/api/vault/missing", None),
        ("GET", "/api/vault/log", None),
    ],
)
async def test_member_cannot_use_privileged_vault_surfaces(client, method, url, body):
    c, app = client
    _act_as(app, MEMBER)

    with patch("brain.systems.vault.async_has_pin", return_value=False):
        response = await c.request(method, url, json=body)

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_org_vault_crud_is_available_to_team_members(client):
    c, app = client
    _act_as(app, MEMBER)

    with patch("brain.systems.vault.async_has_pin", return_value=False), \
         patch("brain.systems.vault.async_list_secrets", return_value=[]) as list_secrets:
        response = await c.get("/api/vault/")

    assert response.status_code == 200
    assert response.json() == []
    list_secrets.assert_called_once_with("user-1", category=None, org_id="org-1")


@pytest.mark.asyncio
async def test_team_member_can_mutate_skill(client):
    c, app = client
    _act_as(app, MEMBER)

    with patch("brain.app.api.routers.skills.SkillRepository") as repo:
        repo.return_value.a_add_guardrail = AsyncMock(return_value=_skill_obj())
        response = await c.post(
            "/api/skills/demo/guardrail",
            json={"text": "Do not leak secrets"},
        )

    assert response.status_code == 200
    repo.return_value.a_add_guardrail.assert_awaited_once_with("demo", "Do not leak secrets", "warning")


@pytest.mark.asyncio
async def test_explicit_scheduler_permission_can_create_scheduler_job(client):
    c, app = client
    _act_as(app, {**MEMBER, "permissions": [PERMISSION_SCHEDULER_MANAGE]})

    with patch("brain.app.api.routers.system.async_upsert_scheduler_job", new=AsyncMock(return_value=SimpleNamespace(job_key="demo", cron_expr="0 8 * * *"))) as upsert_job:
        response = await c.post(
            "/api/system/scheduler/jobs",
            json={"job_key": "demo", "cron_expr": "0 8 * * *", "handler_ref": "python -m demo"},
        )

    assert response.status_code == 200
    assert response.json()["schedule_human"] == "at 8:00 AM"
    upsert_job.assert_awaited_once()


@pytest.mark.asyncio
async def test_legacy_cron_mutation_routes_are_retired(client):
    c, app = client
    _act_as(app, {**MEMBER, "permissions": [PERMISSION_SCHEDULER_MANAGE]})

    post_response = await c.post(
        "/api/system/cron-jobs",
        json={"name": "demo", "schedule": "0 8 * * *", "command": "python -m demo"},
    )
    patch_response = await c.patch("/api/system/cron-jobs/demo", json={"enabled": False})
    delete_response = await c.delete("/api/system/cron-jobs/demo")

    assert post_response.status_code in {404, 405}
    assert patch_response.status_code in {404, 405}
    assert delete_response.status_code in {404, 405}


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["/api/system/embedding", "/api/system/llm"])
async def test_legacy_system_setup_routes_are_removed(client, url):
    c, app = client
    _act_as(app, {**MEMBER, "role": "owner"})

    response = await c.post(url, json={})

    assert response.status_code in {404, 405}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "url", "body"),
    [
        ("POST", "/api/runtime-settings/connection/gemini/api-key", {"api_key": "gemini-key"}),
        ("PATCH", "/api/runtime-settings/memory", {"embedder": "local_cpu", "reranker": "weighted"}),
        ("POST", "/api/runtime-settings/memory/check", None),
    ],
)
async def test_member_cannot_mutate_installation_memory(client, method, url, body):
    c, app = client
    _act_as(app, MEMBER)
    member = SimpleNamespace(id="user-1", org_id="org-1", role="member")

    class RuntimeSettingsDb:
        async def get(self, model, identifier):
            return member if identifier == "user-1" else None

    app.dependency_overrides[get_db] = lambda: RuntimeSettingsDb()
    response = await c.request(method, url, json=body)

    assert response.status_code == 403
