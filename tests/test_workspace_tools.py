from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.dialects.sqlite.base import SQLiteDDLCompiler, SQLiteTypeCompiler

from brain.platform.db.models.org import Org, User
from brain.platform.db.models.workspace_tool import WorkspaceToolInstallation, WorkspaceToolUserConfig
from brain.systems.runs.execution_context import AgentExecutionContext, bind_agent_context
from brain.systems.runs.project_execution_env import prepare_project_execution_env
from brain.systems.runs.tool_catalog.handlers.workspace_tools import _handle_manage_workspace_tools
from brain.systems.runtime_settings.workspace_tools import (
    async_get_workspace_tools_status,
    async_get_workspace_tool_user_config,
    async_install_workspace_tool,
    async_set_workspace_tool_user_config,
    installed_workspace_tool_bundle_ids,
    workspace_tool_catalog,
)


pytestmark = pytest.mark.asyncio

ORG_ID = "11111111-1111-4111-8111-111111111111"
USER_ID = "22222222-2222-4222-8222-222222222222"


def _patch_sqlite_for_pg_types():
    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "TEXT"
    SQLiteTypeCompiler.visit_UUID = lambda self, type_, **kw: "TEXT"

    original = SQLiteDDLCompiler.get_column_default_string

    def patched(self, column, **kw):
        result = original(self, column, **kw)
        if result:
            result = re.sub(r"::jsonb", "", result)
            result = result.replace("NOW()", "CURRENT_TIMESTAMP")
            result = result.replace("gen_random_uuid()", "'00000000-0000-4000-8000-000000000000'")
            result = result.replace("TRUE", "1").replace("FALSE", "0")
        return result

    SQLiteDDLCompiler.get_column_default_string = patched


@pytest.fixture
async def session(async_sqlite_session_factory):
    _patch_sqlite_for_pg_types()
    return await async_sqlite_session_factory(
        [
            Org.__table__,
            User.__table__,
            WorkspaceToolInstallation.__table__,
            WorkspaceToolUserConfig.__table__,
        ]
    )


@pytest.fixture
async def seeded_session(session):
    session.add_all(
        [
            Org(id=ORG_ID, name="Uwear", slug="uwear"),
            User(id=USER_ID, org_id=ORG_ID, name="Reda", email="reda@example.com", approved=True),
        ]
    )
    await session.flush()
    return session


@pytest.fixture
def patch_unit_of_work(monkeypatch, seeded_session):
    class _SessionUnitOfWork:
        async def __aenter__(self):
            self.session = seeded_session
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            if exc_type is None:
                await seeded_session.flush()
            return False

    monkeypatch.setattr(
        "brain.platform.db.repositories.unit_of_work.UnitOfWork",
        _SessionUnitOfWork,
    )


def _workspace_tool_queue(monkeypatch, tmp_path):
    request_file = tmp_path / "workspace-tools" / "request.json"
    status_file = tmp_path / "workspace-tools" / "status.json"
    heartbeat_file = tmp_path / "workspace-tools" / "heartbeat.json"
    log_path = tmp_path / "logs" / "illo-workspace-tools.log"
    root = tmp_path / "tools"
    heartbeat_file.parent.mkdir(parents=True, exist_ok=True)
    heartbeat_file.write_text(
        json.dumps({"status": "ready", "updated_at": datetime.now(timezone.utc).isoformat()}),
        encoding="utf-8",
    )
    monkeypatch.setenv("ILLO_WORKSPACE_TOOLS_REQUEST_FILE", str(request_file))
    monkeypatch.setenv("ILLO_WORKSPACE_TOOLS_STATUS_FILE", str(status_file))
    monkeypatch.setenv("ILLO_WORKSPACE_TOOLS_HEARTBEAT_FILE", str(heartbeat_file))
    monkeypatch.setenv("ILLO_WORKSPACE_TOOLS_LOG_PATH", str(log_path))
    monkeypatch.setenv("ILLO_WORKSPACE_TOOLS_ROOT", str(root))
    return request_file, status_file, root


async def test_workspace_tool_catalog_includes_aws_diagrams():
    bundles = {bundle.id: bundle for bundle in workspace_tool_catalog()}

    assert "aws-diagrams" in bundles
    assert "plantuml" in bundles["aws-diagrams"].provided_commands
    assert "aws-architecture-diagrams" in bundles["aws-diagrams"].skill_dependencies


async def test_workspace_tool_catalog_includes_codex_runtime_auth_profile():
    bundles = {bundle.id: bundle for bundle in workspace_tool_catalog()}

    codex = bundles["codex-cli"]
    profile = codex.runtime["auth_profiles"][0]
    assert "codex" in codex.provided_commands
    assert codex.metadata["npm_package"] == "@openai/codex"
    assert profile["source"] == {
        "type": "provider_connection",
        "provider": "openai",
        "credential": "codex_subscription",
        "scope": "originating_user",
    }
    assert profile["materialize"] == {
        "type": "file",
        "env": "CODEX_HOME",
        "path": "auth.json",
        "format": "codex_auth_json",
    }


async def test_workspace_tool_provider_api_key_accepts_user_openai_connection(monkeypatch):
    from brain.systems.runs import workspace_tool_runtime

    async def fake_resolve_api_key(**kwargs):
        assert kwargs["user_id"] == USER_ID
        assert kwargs["org_id"] == ORG_ID
        assert kwargs["provider"] == "openai"
        assert kwargs["auth_mode"] == "api_key"
        return "sk-user-openai", "user_openai"

    monkeypatch.setattr("brain.systems.vault.async_resolve_api_key", fake_resolve_api_key)

    raw = await workspace_tool_runtime._resolve_provider_connection(
        {
            "provider": "openai",
            "credential": "api_key",
            "scope": "originating_user",
        },
        context={"actor_id": USER_ID, "org_id": ORG_ID},
    )

    assert raw == "sk-user-openai"


async def test_workspace_tool_install_persists_and_queues_request(seeded_session, monkeypatch, tmp_path):
    request_file, status_file, _root = _workspace_tool_queue(monkeypatch, tmp_path)

    status = await async_install_workspace_tool(
        seeded_session,
        org_id=ORG_ID,
        bundle_id="aws-diagrams",
        requested_by=USER_ID,
    )
    await seeded_session.flush()

    request = json.loads(request_file.read_text(encoding="utf-8"))
    queued_status = json.loads(status_file.read_text(encoding="utf-8"))
    row = (
        await seeded_session.execute(
            select(WorkspaceToolInstallation).where(WorkspaceToolInstallation.bundle_id == "aws-diagrams")
        )
    ).scalar_one()

    assert status.status == "running"
    assert status.available is True
    assert request["action"] == "install"
    assert request["org_id"] == ORG_ID
    assert request["bundle_id"] == "aws-diagrams"
    assert queued_status["status"] == "queued"
    assert row.status == "queued"
    assert row.requested_by_user_id == USER_ID
    assert row.bin_path.endswith("/aws-diagrams/current/bin")


async def test_scoped_workspace_tool_status_does_not_duplicate_other_manifest_rows(
    seeded_session,
    monkeypatch,
    tmp_path,
):
    _request_file, _status_file, root = _workspace_tool_queue(monkeypatch, tmp_path)
    aws_paths_root = root / "orgs" / ORG_ID / "aws-diagrams" / "current"
    aws_bin = aws_paths_root / "bin"
    aws_bin.mkdir(parents=True)
    (aws_paths_root / "illo-tool.json").write_text(
        json.dumps({
            "bundle_id": "aws-diagrams",
            "name": "AWS Architecture Diagrams",
            "status": "installed",
            "path_entries": [str(aws_bin)],
        }),
        encoding="utf-8",
    )
    seeded_session.add(
        WorkspaceToolInstallation(
            org_id=ORG_ID,
            bundle_id="aws-diagrams",
            display_name="AWS Architecture Diagrams",
            status="installed",
            bin_path=str(aws_bin),
        )
    )
    await seeded_session.flush()

    status = await async_get_workspace_tools_status(
        seeded_session,
        org_id=ORG_ID,
        bundle_id="codex-cli",
    )
    await seeded_session.flush()
    rows = (
        await seeded_session.execute(
            select(WorkspaceToolInstallation).where(WorkspaceToolInstallation.bundle_id == "aws-diagrams")
        )
    ).scalars().all()

    assert status.catalog[0].id == "aws-diagrams"
    assert [row.bundle_id for row in rows] == ["aws-diagrams"]


async def test_manage_workspace_tools_handler_queues_install(
    seeded_session,
    patch_unit_of_work,
    monkeypatch,
    tmp_path,
):
    request_file, _status_file, _root = _workspace_tool_queue(monkeypatch, tmp_path)

    with bind_agent_context(AgentExecutionContext(user_id=USER_ID, org_id=ORG_ID)):
        payload = json.loads(await _handle_manage_workspace_tools(action="install", bundle_id="aws-diagrams"))

    request = json.loads(request_file.read_text(encoding="utf-8"))
    assert payload["action"] == "install"
    assert payload["status"] == "running"
    assert request["bundle_id"] == "aws-diagrams"


async def test_installed_workspace_tool_paths_are_injected_into_command_env(monkeypatch, tmp_path):
    root = tmp_path / "tools"
    bin_path = root / "orgs" / ORG_ID / "aws-diagrams" / "current" / "bin"
    bin_path.mkdir(parents=True)
    manifest_path = bin_path.parent / "illo-tool.json"
    manifest_path.write_text(
        json.dumps({
            "status": "installed",
            "path_entries": [str(bin_path)],
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("ILLO_WORKSPACE_TOOLS_ROOT", str(root))

    with bind_agent_context(AgentExecutionContext(org_id=ORG_ID)):
        execution_env = prepare_project_execution_env()

    assert execution_env.env is not None
    assert execution_env.env["PATH"].split(os.pathsep)[0] == str(bin_path)
    assert execution_env.env["ILLO_WORKSPACE_TOOLS_PATH"] == str(bin_path)


async def test_installed_workspace_tool_bundle_ids_read_installed_manifests(monkeypatch, tmp_path):
    root = tmp_path / "tools"
    bin_path = root / "orgs" / ORG_ID / "codex-cli" / "current" / "bin"
    bin_path.mkdir(parents=True)
    manifest_path = bin_path.parent / "illo-tool.json"
    manifest_path.write_text(
        json.dumps({
            "bundle_id": "codex-cli",
            "status": "installed",
            "path_entries": [str(bin_path)],
        }),
        encoding="utf-8",
    )
    monkeypatch.setenv("ILLO_WORKSPACE_TOOLS_ROOT", str(root))

    assert installed_workspace_tool_bundle_ids(ORG_ID) == ["codex-cli"]


async def test_workspace_tool_user_config_persists_non_secret_refs(seeded_session):
    saved = await async_set_workspace_tool_user_config(
        seeded_session,
        org_id=ORG_ID,
        user_id=USER_ID,
        bundle_id="codex-cli",
        preferences={"default_model": "gpt-5.1-codex", "approval_mode": "suggest"},
        credential_refs={"openai": {"type": "provider_connection", "credential": "codex_subscription"}},
    )

    loaded = await async_get_workspace_tool_user_config(
        seeded_session,
        org_id=ORG_ID,
        user_id=USER_ID,
        bundle_id="codex-cli",
    )

    assert loaded is not None
    assert saved.id == loaded.id
    assert loaded.preferences["approval_mode"] == "suggest"
    assert loaded.credential_refs["openai"]["credential"] == "codex_subscription"
