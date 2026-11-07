"""Human-only policy changes, organization isolation, and atomic audit writes."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select

from brain.app.api.auth import get_current_user
from brain.app.api.deps import get_db, rate_limit
from brain.app.api.routers import tool_policies
from brain.platform.db.models.org import Org, User
from brain.platform.db.models.tool_policy import ToolPolicy, ToolPolicyChangeAudit
from brain.systems.runs.tool_policy_store import (
    load_tool_policies as load_saved_policies,
    apply_tool_policy_change,
    read_tool_policy,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def workspace(async_sqlite_session_factory):
    session = await async_sqlite_session_factory(
        [
            Org.__table__,
            User.__table__,
            ToolPolicy.__table__,
            ToolPolicyChangeAudit.__table__,
        ]
    )
    org_id, other_org_id, user_id = (str(uuid4()) for _ in range(3))
    session.add_all(
        [
            Org(id=org_id, name="Workspace", slug=f"workspace-{org_id}"),
            Org(
                id=other_org_id,
                name="Other workspace",
                slug=f"workspace-{other_org_id}",
            ),
            User(
                id=user_id,
                org_id=org_id,
                name="Admin",
                email=f"{user_id}@example.com",
                role="admin",
            ),
        ]
    )
    await session.commit()
    user = {"id": user_id, "org_id": org_id, "principal_type": "human", "role": "admin"}
    app = FastAPI()
    app.include_router(tool_policies.router)

    async def db():
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise

    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = db
    app.dependency_overrides[rate_limit] = lambda: None
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield session, client, user, other_org_id


@pytest.mark.parametrize(
    "identity",
    [
        {"role": "member", "principal_type": "human"},
        {"role": "owner", "principal_type": "service"},
        {"role": "admin", "principal_type": "api_key"},
        {"role": "admin", "principal_type": "agent"},
    ],
)
async def test_non_human_admin_cannot_put(workspace, identity):
    session, client, user, _ = workspace
    user.update(identity)
    for path in ("organization", "skills/review"):
        response = await client.put(
            f"/api/tool-policies/{path}",
            json={"disabled_tools": ["exec_command"], "expected_version": 0},
        )
        assert response.status_code == 403
    assert (
        await session.scalar(select(func.count()).select_from(ToolPolicyChangeAudit))
        == 0
    )


async def test_admin_put_saves_unknown_names_and_audit_then_detects_conflict(workspace):
    session, client, user, _ = workspace
    response = await client.put(
        "/api/tool-policies/organization",
        json={
            "disabled_tools": [" exec_command ", "future_tool", "exec_command"],
            "expected_version": 0,
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["disabled_tools"] == ["exec_command", "future_tool"]
    assert data["version"] == 1
    assert data["updated_by"] == user["id"]
    assert data["updated_at"]
    assert [item["name"] for item in data["warnings"]] == ["future_tool"]
    assert any(
        tool["name"] == "exec_command" and tool["description"] for tool in data["tools"]
    )
    history = (await client.get("/api/tool-policies/organization/history")).json()
    assert len(history) == 1
    assert history[0]["actor_id"] == user["id"]
    assert history[0]["actor_type"] == "human"
    assert history[0]["before_disabled_tools"] == []
    assert history[0]["after_disabled_tools"] == data["disabled_tools"]
    assert history[0]["changed_at"]
    conflict = await client.put(
        "/api/tool-policies/organization",
        json={"disabled_tools": [], "expected_version": 0},
    )
    assert conflict.status_code == 409
    assert (
        await session.scalar(select(func.count()).select_from(ToolPolicyChangeAudit))
        == 1
    )
    updated = await client.put(
        "/api/tool-policies/organization",
        json={"disabled_tools": [], "expected_version": 1},
    )
    assert updated.status_code == 200
    history = (await client.get("/api/tool-policies/organization/history")).json()
    assert history[0]["version"] == 2
    assert history[0]["before_disabled_tools"] == ["exec_command", "future_tool"]
    assert history[0]["after_disabled_tools"] == []


async def test_members_can_read_only_their_org_and_its_history(workspace):
    session, client, user, other_org = workspace
    await apply_tool_policy_change(
        session,
        org_id=other_org,
        scope_type="organization",
        disabled_tools=["exec_command"],
        expected_version=0,
        actor_id=user["id"],
        actor_type="human",
    )
    await session.commit()
    user["role"] = "member"
    response = await client.get("/api/tool-policies/organization")
    assert response.status_code == 200
    assert response.json()["disabled_tools"] == []
    assert response.json()["version"] == 0
    assert (await client.get("/api/tool-policies/organization/history")).json() == []
    user.pop("org_id")
    assert (await client.get("/api/tool-policies/organization")).status_code == 403


async def test_skill_policy_checks_visibility_and_is_scoped_per_org(
    workspace, monkeypatch
):
    session, client, user, other_org = workspace
    visible = []

    async def get_visible(_self, **kwargs):
        visible.append(kwargs)
        return object() if kwargs["name"] == "review" else None

    monkeypatch.setattr(tool_policies.SkillRepository, "a_get_visible", get_visible)
    assert (
        await client.put(
            "/api/tool-policies/skills/missing",
            json={"disabled_tools": [], "expected_version": 0},
        )
    ).status_code == 404
    response = await client.put(
        "/api/tool-policies/skills/review",
        json={"disabled_tools": ["run_script"], "expected_version": 0},
    )
    assert response.status_code == 200
    assert visible[-1] == {
        "org_id": user["org_id"],
        "user_id": user["id"],
        "name": "review",
    }
    assert (await client.get("/api/tool-policies/skills/review/history")).json()[0][
        "scope_key"
    ] == "review"
    assert (
        await read_tool_policy(
            session, org_id=other_org, scope_type="skill", scope_key="review"
        )
        is None
    )
    assert (await client.get("/api/tool-policies/organization")).json()[
        "disabled_tools"
    ] == []


@pytest.mark.parametrize("names", [[" "], ["x" * 201], ["tool"] * 513, [None]])
async def test_invalid_tool_names_are_rejected(workspace, names):
    _, client, _, _ = workspace
    response = await client.put(
        "/api/tool-policies/organization",
        json={"disabled_tools": names, "expected_version": 0},
    )
    assert response.status_code == 422


async def test_policy_and_audit_rollback_together(workspace):
    session, _, user, _ = workspace
    await apply_tool_policy_change(
        session,
        org_id=user["org_id"],
        scope_type="organization",
        disabled_tools=["exec_command"],
        expected_version=0,
        actor_id=user["id"],
        actor_type="human",
    )
    await session.rollback()
    assert (
        await read_tool_policy(
            session, org_id=user["org_id"], scope_type="organization"
        )
        is None
    )
    assert (
        await session.scalar(select(func.count()).select_from(ToolPolicyChangeAudit))
        == 0
    )


async def test_saved_policy_loader_reads_only_the_org_and_requested_skills(
    workspace, monkeypatch
):
    from contextlib import asynccontextmanager
    from brain.platform import db as database

    session, _, user, other_org = workspace
    for org_id, scope, key, names in (
        (user["org_id"], "organization", "", ["exec_command"]),
        (user["org_id"], "skill", "review", ["run_script"]),
        (user["org_id"], "skill", "other", ["write_file"]),
        (other_org, "skill", "review", ["read_file"]),
    ):
        await apply_tool_policy_change(
            session,
            org_id=org_id,
            scope_type=scope,
            scope_key=key,
            disabled_tools=names,
            expected_version=0,
            actor_id=user["id"],
            actor_type="human",
        )
    await session.commit()

    @asynccontextmanager
    async def factory():
        yield session

    monkeypatch.setattr(database, "SessionFactory", factory)
    rows = await load_saved_policies(user["org_id"], {"review"})
    assert {(row.scope_type, row.scope_key) for row in rows} == {
        ("organization", ""),
        ("skill", "review"),
    }
