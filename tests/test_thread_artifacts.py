from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects.sqlite.base import SQLiteDDLCompiler, SQLiteTypeCompiler

from brain.platform.db.models.idea import Idea
from brain.platform.db.models.org import Org, User
from brain.platform.db.models.workspace_app import WorkspaceApp, WorkspaceAppState, WorkspaceAppVersion

ORG_ID = "aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaa"
USER_ID = "bbbbbbbbbbbb4bbb8bbbbbbbbbbbbbbb"
THREAD_ID = "cccccccccccc4ccc8ccccccccccccccc"

VALID_ARTIFACT_SOURCE = """
<main class="illo-app">
  <section class="illo-panel illo-stack">
    <div class="illo-toolbar">
      <h1 class="illo-title">Brainstorm Brief</h1>
      <button class="illo-button" id="vote">Vote</button>
    </div>
    <p class="illo-copy" id="result">Ready for review.</p>
  </section>
</main>
<script>
  document.getElementById('vote').addEventListener('click', async () => {
    const state = await window.illo.state.get();
    const votes = Number(state.votes || 0) + 1;
    await window.illo.state.update({ votes });
    document.getElementById('result').textContent = votes + ' vote(s)';
  });
</script>
"""


def _register_sqlite_functions(dbapi_conn, _connection_record) -> None:
    dbapi_conn.create_function("NOW", 0, lambda: datetime.now(timezone.utc).isoformat())
    dbapi_conn.create_function("gen_random_uuid", 0, lambda: str(uuid.uuid4()))


def _patch_sqlite_for_pg_types() -> None:
    for name in ("visit_JSONB", "visit_ARRAY", "visit_UUID", "visit_VECTOR", "visit_Vector"):
        if not hasattr(SQLiteTypeCompiler, name):
            setattr(SQLiteTypeCompiler, name, lambda self, type_, **kw: "TEXT")
    SQLiteTypeCompiler.visit_BIGINT = lambda self, type_, **kw: "INTEGER"

    original = SQLiteDDLCompiler.get_column_default_string
    if getattr(original, "_thread_artifact_patch", False):
        return

    def patched(self, column, **kw):
        result = original(self, column, **kw)
        if result:
            result = re.sub(r"::jsonb", "", result)
            result = result.replace("NOW()", "CURRENT_TIMESTAMP")
        return result

    patched._thread_artifact_patch = True
    SQLiteDDLCompiler.get_column_default_string = patched


@pytest.fixture
async def session(async_sqlite_session_factory):
    _patch_sqlite_for_pg_types()
    sqlite3.register_adapter(dict, lambda value: json.dumps(value))
    sqlite3.register_adapter(list, lambda value: json.dumps(value))
    db = await async_sqlite_session_factory(
        [
            Org.__table__,
            User.__table__,
            Idea.__table__,
            WorkspaceApp.__table__,
            WorkspaceAppVersion.__table__,
            WorkspaceAppState.__table__,
        ],
        connect_listener=_register_sqlite_functions,
    )
    db.add_all(
        [
            Org(id=ORG_ID, name="Test Org", slug="test"),
            User(id=USER_ID, org_id=ORG_ID, name="Alex", email="alex@example.test"),
            Idea(id=THREAD_ID, title="Artifact source thread", user_id=USER_ID, org_id=ORG_ID),
        ]
    )
    await db.flush()
    return db


async def test_publish_thread_artifact_creates_and_republishes_app_capsule(session):
    from brain.systems.cortex.thread_artifacts import publish_thread_artifact_app

    created = await publish_thread_artifact_app(
        session,
        org_id=ORG_ID,
        user_id=USER_ID,
        thread_id=THREAD_ID,
        title="Brainstorm Brief",
        description="Interactive team review",
        artifact_kind="brainstorm",
        source_code=VALID_ARTIFACT_SOURCE,
        initial_state={"votes": 0},
    )

    assert created["action"] == "created"
    assert created["artifact_route"] == f"/threads/{THREAD_ID}?app={created['app_id']}"
    assert created["artifact_url"].endswith(created["artifact_route"])
    assert created["app"]["metadata"]["artifact_scope"] == "thread"
    assert created["app"]["metadata"]["thread_artifact"]["thread_id"] == THREAD_ID
    assert created["app"]["active_version"]["manifest"]["thread_artifact"]["kind"] == "brainstorm"
    assert created["app"]["active_version"]["manifest"]["state_key"] == f"thread-artifact-{THREAD_ID}"
    assert created["app"]["active_version"]["source_kind"] == "html"
    assert created["version"] == 1

    updated = await publish_thread_artifact_app(
        session,
        org_id=ORG_ID,
        user_id=USER_ID,
        thread_id=THREAD_ID,
        title="Brainstorm Brief",
        artifact_kind="brainstorm",
        source_code=VALID_ARTIFACT_SOURCE.replace("Ready for review.", "Republished."),
    )

    assert updated["action"] == "updated"
    assert updated["app_id"] == created["app_id"]
    assert updated["version"] == 2
    assert "Republished." in updated["app"]["active_version"]["source_code"]


async def test_mcp_thread_artifact_publish_capability_sets_workspace_app_mutation(monkeypatch, session):
    from brain.app.api.routers import agent_mcp

    calls: list[dict] = []

    async def fake_publish(db, **kwargs):
        calls.append({"db": db, **kwargs})
        return {
            "action": "created",
            "app_id": "app-1",
            "app_key": "thread-artifact",
            "app": {"id": "app-1", "key": "thread-artifact"},
        }

    monkeypatch.setattr(
        "brain.systems.cortex.thread_artifacts.publish_thread_artifact_app",
        fake_publish,
    )
    principal = SimpleNamespace(org_id=ORG_ID, owner_user_id=USER_ID)

    result = await agent_mcp._tool_act(
        session,
        principal,
        {
            "capability": "thread.artifact.publish",
            "arguments": {
                "thread_id": THREAD_ID,
                "title": "Review artifact",
                "artifact_kind": "status",
                "source_code": VALID_ARTIFACT_SOURCE,
            },
        },
    )

    assert "thread.artifact.publish" in agent_mcp.ACT_CAPABILITIES
    assert result["_mutates_workspace_app"] is True
    assert result["_workspace_app_change"] == {"action": "created", "app": {"id": "app-1", "key": "thread-artifact"}}
    assert calls[0]["db"] is session
    assert calls[0]["org_id"] == ORG_ID
    assert calls[0]["user_id"] == USER_ID
    assert calls[0]["thread_id"] == THREAD_ID
    assert calls[0]["metadata"]["mcp_capability"] == "thread.artifact.publish"
