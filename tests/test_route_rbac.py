"""Focused RBAC coverage for privileged non-Cortex API routes."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from brain.app.api.auth import get_current_user
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
        ("GET", "/api/vault/org-users", None),
        ("DELETE", "/api/vault/shares/1", None),
        ("GET", "/api/vault/missing", None),
        ("GET", "/api/vault/log", None),
        ("POST", "/api/vault/1/share", {"shared_with_user_id": "user-2"}),
    ],
)
async def test_member_cannot_use_privileged_vault_surfaces(client, method, url, body):
    c, app = client
    _act_as(app, MEMBER)

    with patch("brain.systems.vault.async_has_pin", return_value=False):
        response = await c.request(method, url, json=body)

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_personal_vault_crud_remains_user_owned(client):
    c, app = client
    _act_as(app, MEMBER)

    with patch("brain.systems.vault.async_has_pin", return_value=False), \
         patch("brain.systems.vault.async_list_secrets", return_value=[]) as list_secrets:
        response = await c.get("/api/vault/")

    assert response.status_code == 200
    assert response.json() == []
    list_secrets.assert_called_once_with("user-1", category=None, org_id="org-1")


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
