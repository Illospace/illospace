"""MCP settings authorization and org-scoped persistence."""
from __future__ import annotations

import json
import uuid

from fastapi import FastAPI
import httpx
import pytest
from sqlalchemy import select

from brain.app.api.auth import get_current_user
from brain.app.api.deps import get_db
from brain.app.api.routers.external_mcp import router
from brain.platform.db.models.external_mcp import ExternalMcpAuditEvent, ExternalMcpServer
from brain.platform.db.models.org import Org, User
from brain.systems.external_mcp import runtime
from tests.external_mcp_support import CLIENT_SECRET, mcp_server  # noqa: F401


def _app(user, session=None):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: user
    async def db():
        yield session
    app.dependency_overrides[get_db] = db
    return app


@pytest.mark.parametrize("method,path,payload", [
    ("POST", "/", {"name": "reports", "url": "https://mcp.example.com/mcp"}),
    ("PATCH", "/1", {"enabled": False}), ("DELETE", "/1", None), ("POST", "/1/test", None),
])
async def test_external_mcp_non_admin_forbidden(method, path, payload):
    app = _app({"id": str(uuid.uuid4()), "org_id": str(uuid.uuid4()), "role": "member"})
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        response = await client.request(method, "/api/external-mcp/servers" + path, json=payload)
    assert response.status_code == 403
    assert CLIENT_SECRET not in response.text


@pytest.fixture
async def org_admin(db_session):
    org = Org(name="Example", slug=f"example-{uuid.uuid4().hex}")
    db_session.add(org)
    await db_session.flush()
    user = User(org_id=org.id, name="Admin", email=f"{uuid.uuid4().hex}@example.com", role="admin")
    db_session.add(user)
    await db_session.flush()
    return {"id": user.id, "org_id": org.id, "role": "admin"}


@pytest.mark.requires_db
async def test_external_mcp_org_api_crud_and_audit(db_session, org_admin, mcp_server):
    app = _app(org_admin, db_session)
    base = "/api/external-mcp/servers"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        assert (await client.get(base + "/")).json() == []
        created = await client.post(base + "/", json={"name": "reports", "url": mcp_server.url,
                "auth_mode": "bearer", "bearer_token_key": "reports_token", "allowed_tools": ["get_report"]})
        assert created.status_code == 201, created.text
        data = created.json()
        assert data["missing_secret_keys"] == ["reports_token"]
        server_id = data["id"]
        updated = await client.patch(f"{base}/{server_id}", json={"auth_mode": "none"})
        assert updated.status_code == 200
        assert updated.json()["bearer_token_key"] is None
        mcp_server.require_auth = False
        tested = await client.post(f"{base}/{server_id}/test")
        assert tested.json()["ok"] is True
        assert [tool["allowed"] for tool in tested.json()["tools"]] == [True, False]
        duplicate = await client.post(base + "/", json={"name": "reports", "url": mcp_server.url})
        assert duplicate.status_code == 422
        assert CLIENT_SECRET not in json.dumps([data, updated.json(), tested.json()])
        deleted = await client.delete(f"{base}/{server_id}")
        assert deleted.status_code == 200
        assert (await client.get(base + "/")).json() == []
    events = (await db_session.scalars(select(ExternalMcpAuditEvent).where(ExternalMcpAuditEvent.org_id == org_admin["org_id"]).order_by(ExternalMcpAuditEvent.id))).all()
    assert [event.event_type for event in events] == ["server_created", "server_updated", "connection_tested", "server_deleted"]
    await db_session.refresh(events[0])
    assert events[0].server_id is None
    assert "name" in events[0].changed_fields
    assert "bearer_token_key" in events[1].changed_fields
    assert not {"arguments", "result", "token", "error_message"} & set(ExternalMcpAuditEvent.__table__.columns.keys())


@pytest.mark.requires_db
async def test_external_mcp_cross_org_not_found(db_session, org_admin, mcp_server):
    other = Org(name="Other", slug=f"other-{uuid.uuid4().hex}")
    db_session.add(other)
    await db_session.flush()
    server = ExternalMcpServer(org_id=other.id, name="support", url=mcp_server.url)
    db_session.add(server)
    await db_session.flush()
    app = _app(org_admin, db_session)
    base = f"/api/external-mcp/servers/{server.id}"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        assert (await client.get("/api/external-mcp/servers/")).json() == []
        for method, path, payload in [("PATCH", base, {"enabled": False}), ("DELETE", base, None), ("POST", base + "/test", None)]:
            assert (await client.request(method, path, json=payload)).status_code == 404


@pytest.mark.requires_db
async def test_external_mcp_tool_audit_has_no_payload(db_session, org_admin, mcp_server, monkeypatch, unit_of_work_for_session):
    from brain.systems.external_mcp.config import save_server
    monkeypatch.setattr(runtime, "UnitOfWork", unit_of_work_for_session)
    server = await save_server(db_session, org_id=org_admin["org_id"], actor_user_id=org_admin["id"],
                               values={"name": "reports", "url": mcp_server.url, "allowed_tools": ["get_report"]})
    mcp_server.require_auth = False
    tools, handlers, _ = await runtime.external_mcp_tool_surface(org_id=org_admin["org_id"], run_id=None, actor_user_id=org_admin["id"])
    assert tools[0]["name"] == "mcp__reports__get_report"
    await handlers[tools[0]["name"]](id="private-argument-marker")
    await db_session.flush()
    event = await db_session.scalar(select(ExternalMcpAuditEvent).where(ExternalMcpAuditEvent.server_id == server.id, ExternalMcpAuditEvent.event_type == "tool_called"))
    assert event is not None and event.outcome == "ok" and event.duration_ms >= 0
    stored = {column.name: getattr(event, column.name) for column in event.__table__.columns}
    text = json.dumps(stored, default=str)
    assert "private-argument-marker" not in text and "Report data" not in text


@pytest.mark.requires_db
async def test_external_mcp_real_vault_service_credentials(db_session, org_admin, mcp_server, monkeypatch, unit_of_work_for_session, caplog):
    import logging
    from cryptography.fernet import Fernet
    from brain.systems import vault
    from brain.systems.external_mcp import tokens
    from brain.systems.external_mcp.config import save_server
    from brain.platform.db.models.vault import VaultAgentGrant
    from tests.external_mcp_support import CLIENT_ID

    monkeypatch.setenv("VAULT_MASTER_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(vault, "UnitOfWork", unit_of_work_for_session)
    monkeypatch.setattr(runtime, "UnitOfWork", unit_of_work_for_session)
    monkeypatch.setattr(tokens, "TOKEN_CACHE", tokens.TokenCache())
    runtime._TOOL_CACHE.clear()
    for key, value in [("client_id", CLIENT_ID), ("client_secret", CLIENT_SECRET)]:
        await vault.set_secret(key, value, org_admin["id"], org_id=org_admin["org_id"], agent_access_level="ask")
    server = await save_server(db_session, org_id=org_admin["org_id"], actor_user_id=org_admin["id"], values={
        "name": "reports", "url": mcp_server.url, "auth_mode": "client_credentials", "token_url": mcp_server.token_url,
        "client_id_key": "client_id", "client_secret_key": "client_secret", "allowed_tools": ["get_report"],
        "scope": "reports.read", "resource": "https://mcp.example.com/",
    })
    caplog.set_level(logging.DEBUG)
    app = _app(org_admin, db_session)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        listed = await client.get("/api/external-mcp/servers/")
        assert listed.json()[0]["missing_secret_keys"] == []
        tested = await client.post(f"/api/external-mcp/servers/{server.id}/test")
    assert tested.json()["ok"] is True
    tools, handlers, _ = await runtime.external_mcp_tool_surface(org_id=org_admin["org_id"], run_id=None, actor_user_id=org_admin["id"])
    assert (await handlers[tools[0]["name"]]())["content"] == "Report data"
    assert mcp_server.token_requests == 1
    assert await db_session.scalar(select(VaultAgentGrant).where(VaultAgentGrant.org_id == org_admin["org_id"])) is None
    for value in (CLIENT_SECRET, mcp_server.token):
        assert value not in listed.text and value not in tested.text and value not in caplog.text
