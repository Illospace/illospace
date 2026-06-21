from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite.base import SQLiteDDLCompiler, SQLiteTypeCompiler

from brain.platform.db.models.org import Org, User
from brain.platform.db.models.workspace_app import (
    WorkspaceApp,
    WorkspaceAppEvent,
    WorkspaceAppState,
    WorkspaceAppVersion,
)
from brain.systems.workspace_apps.collaboration import (
    a_append_collaboration_event,
    a_get_collaboration_snapshot,
    a_list_collaboration_events,
)
from brain.systems.workspace_apps.service import WorkspaceAppError, a_create_app

ORG_ID = "11111111-1111-4111-8111-111111111111"
USER_ID = "22222222-2222-4222-8222-222222222222"

VALID_SOURCE = """
<main class="illo-app">
  <section class="illo-panel illo-stack">
    <h1 class="illo-title">Team Decision</h1>
    <button class="illo-button">Vote</button>
  </section>
</main>
"""


def _register_sqlite_functions(dbapi_conn, _connection_record) -> None:
    dbapi_conn.create_function("NOW", 0, lambda: datetime.now(timezone.utc).isoformat())
    dbapi_conn.create_function("gen_random_uuid", 0, lambda: str(uuid.uuid4()))


def _patch_sqlite_for_pg_types() -> None:
    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "TEXT"
    SQLiteTypeCompiler.visit_UUID = lambda self, type_, **kw: "TEXT"

    original = SQLiteDDLCompiler.get_column_default_string
    if getattr(original, "_workspace_app_collab_patch", False):
        return

    def patched(self, column, **kw):
        result = original(self, column, **kw)
        if result:
            result = re.sub(r"::jsonb", "", result)
            result = result.replace("NOW()", "CURRENT_TIMESTAMP")
        return result

    patched._workspace_app_collab_patch = True
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
            WorkspaceApp.__table__,
            WorkspaceAppVersion.__table__,
            WorkspaceAppState.__table__,
            WorkspaceAppEvent.__table__,
        ],
        connect_listener=_register_sqlite_functions,
    )
    db.add_all(
        [
            Org(id=ORG_ID, name="Test Org", slug="test"),
            User(id=USER_ID, org_id=ORG_ID, name="Alex", email="alex@example.test"),
        ]
    )
    await db.flush()
    return db


def _collaboration_manifest():
    return {
        "contract_version": 1,
        "state_key": "decision-board",
        "data_plan": {"mode": "capability"},
        "collaboration": {
            "mode": "event_sourced",
            "actions": {
                "vote.cast": {
                    "description": "Record or update one participant vote.",
                    "reducer": {
                        "type": "choice_by_actor",
                        "state_path": "votes",
                        "value_field": "optionId",
                    },
                },
                "note.add": {
                    "description": "Append one participant note.",
                    "reducer": {"type": "append", "state_path": "notes"},
                },
                "status.change": {
                    "description": "Apply a host-validated status patch.",
                },
            },
        },
        "design_contract": {
            "kit": "constellation-app-kit",
            "theme_modes": ["dark", "light"],
        },
    }


async def _create_collaboration_app(session):
    return await a_create_app(
        session,
        org_id=ORG_ID,
        key="decision-board",
        name="Decision Board",
        renderer_key="app-capsule",
        source_kind="html",
        source_code=VALID_SOURCE,
        manifest=_collaboration_manifest(),
        visual_spec={
            "thumbnail": {
                "label": "Decision",
                "value": "Open",
                "secondary": "Collaborative artifact",
            }
        },
        initial_state={"votes": {}, "notes": []},
        state_key="decision-board",
        created_by_user_id=USER_ID,
    )


async def _count_events(session) -> int:
    return int(await session.scalar(select(func.count()).select_from(WorkspaceAppEvent)))


async def test_collaboration_event_applies_declared_reducer_and_lists_events(session):
    app = await _create_collaboration_app(session)

    result = await a_append_collaboration_event(
        session,
        org_id=ORG_ID,
        app_id=app.id,
        event_type="vote.cast",
        payload={"optionId": "ship-collab-runtime", "confidence": 0.9},
        idempotency_key="vote-user-1",
        user_id=USER_ID,
    )

    assert result["duplicate"] is False
    assert result["state"]["version"] == 1
    assert result["state"]["data"]["votes"][f"user:{USER_ID}"]["value"] == "ship-collab-runtime"
    assert result["events"][0]["event_type"] == "vote.cast"
    assert result["events"][0]["state_version"] == 1

    events = await a_list_collaboration_events(session, org_id=ORG_ID, app_id=app.id)
    assert [event.event_type for event in events] == ["vote.cast"]

    snapshot = await a_get_collaboration_snapshot(session, org_id=ORG_ID, app_id=app.id)
    assert snapshot["state"]["data"]["votes"][f"user:{USER_ID}"]["payload"]["confidence"] == 0.9
    assert snapshot["events"][0]["id"] == result["events"][0]["id"]


async def test_collaboration_accepts_reducer_shorthand_manifest(session):
    manifest = _collaboration_manifest()
    manifest["collaboration"]["actions"]["vote.cast"] = {
        "description": "Record or update one participant vote.",
        "reducer": "choice_by_actor",
        "state_key": "votes",
        "value_field": "optionId",
    }
    manifest["collaboration"]["actions"]["note.add"] = {
        "description": "Append one participant note.",
        "reducer": "append",
        "list_key": "notes",
    }

    app = await a_create_app(
        session,
        org_id=ORG_ID,
        key="shorthand-decision-board",
        name="Shorthand Decision Board",
        renderer_key="app-capsule",
        source_kind="html",
        source_code=VALID_SOURCE,
        manifest=manifest,
        visual_spec={"thumbnail": {"label": "Decision", "value": "Open"}},
        initial_state={"votes": {}, "notes": []},
        state_key="decision-board",
        created_by_user_id=USER_ID,
    )

    vote = await a_append_collaboration_event(
        session,
        org_id=ORG_ID,
        app_id=app.id,
        event_type="vote.cast",
        payload={"optionId": "decision-board"},
        user_id=USER_ID,
    )
    note = await a_append_collaboration_event(
        session,
        org_id=ORG_ID,
        app_id=app.id,
        event_type="note.add",
        payload={"text": "The board makes feedback concrete."},
        user_id=USER_ID,
    )

    assert vote["state"]["data"]["votes"][f"user:{USER_ID}"]["value"] == "decision-board"
    assert note["state"]["data"]["notes"] == [{"text": "The board makes feedback concrete."}]


async def test_collaboration_event_idempotency_returns_existing_event(session):
    app = await _create_collaboration_app(session)

    first = await a_append_collaboration_event(
        session,
        org_id=ORG_ID,
        app_id=app.id,
        event_type="note.add",
        payload={"body": "Prefer the generic runtime."},
        idempotency_key="note-1",
        user_id=USER_ID,
    )
    second = await a_append_collaboration_event(
        session,
        org_id=ORG_ID,
        app_id=app.id,
        event_type="note.add",
        payload={"body": "Duplicate should not append."},
        idempotency_key="note-1",
        user_id=USER_ID,
    )

    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert await _count_events(session) == 1
    assert second["state"]["data"]["notes"] == [{"body": "Prefer the generic runtime."}]


async def test_collaboration_event_can_apply_explicit_state_patch(session):
    app = await _create_collaboration_app(session)

    result = await a_append_collaboration_event(
        session,
        org_id=ORG_ID,
        app_id=app.id,
        event_type="status.change",
        payload={"reason": "quorum"},
        state_patch={"status": "ready_for_illo"},
        user_id=USER_ID,
    )

    assert result["state"]["data"]["status"] == "ready_for_illo"
    assert result["events"][0]["state_patch"] == {"status": "ready_for_illo"}


async def test_collaboration_event_rejects_undeclared_event_type(session):
    app = await _create_collaboration_app(session)

    with pytest.raises(WorkspaceAppError, match="not declared"):
        await a_append_collaboration_event(
            session,
            org_id=ORG_ID,
            app_id=app.id,
            event_type="unknown.action",
            payload={},
            user_id=USER_ID,
        )
