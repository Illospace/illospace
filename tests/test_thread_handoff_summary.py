from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.dialects.sqlite.base import SQLiteDDLCompiler, SQLiteTypeCompiler

from brain.platform.db.models.agent import AgentSession
from brain.platform.db.models.agent_run import AgentRunRow


IDEA_ID = "33333333-3333-3333-3333-333333333333"


@pytest.fixture
async def session(async_sqlite_session_factory):
    _patch_sqlite_for_pg_types()
    sqlite3.register_adapter(dict, lambda value: json.dumps(value))
    sqlite3.register_adapter(list, lambda value: json.dumps(value))
    return await async_sqlite_session_factory(
        [
            AgentRunRow.__table__,
            AgentSession.__table__,
        ],
        connect_listener=_register_sqlite_functions,
    )


async def test_latest_thread_handoff_summary_returns_newest_thread_session(session):
    from brain.systems.runs.cortex.handoff_summary import latest_thread_handoff_summary

    now = datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc)
    older_handoff = _handoff_payload("Older objective", message_count=4)
    newest_handoff = _handoff_payload("Ship the handoff summary tab", message_count=8)
    unrelated_handoff = _handoff_payload("Do not leak this thread", message_count=12)

    session.add_all(
        [
            _run(11, IDEA_ID, now - timedelta(minutes=30)),
            _run(12, IDEA_ID, now - timedelta(minutes=10)),
            _run(13, "other-idea", now),
            _agent_session(
                "agent-run-11",
                older_handoff,
                handoff_updated_at=now - timedelta(minutes=20),
            ),
            _agent_session(
                "agent-run-12-worker",
                newest_handoff,
                handoff_updated_at=now - timedelta(minutes=5),
            ),
            _agent_session(
                "agent-run-13",
                unrelated_handoff,
                handoff_updated_at=now,
            ),
        ]
    )
    await session.flush()

    summary = await latest_thread_handoff_summary(session, IDEA_ID)

    assert summary["found"] is True
    assert summary["run_id"] == 12
    assert summary["session_id"] == "agent-run-12-worker"
    assert summary["message_count"] == 8
    assert summary["summary"]["checkpoint"]["active_objective"] == "Ship the handoff summary tab"


async def test_latest_thread_handoff_summary_reports_empty_thread(session):
    from brain.systems.runs.cortex.handoff_summary import latest_thread_handoff_summary

    session.add(_run(21, IDEA_ID, datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc)))
    session.add(
        AgentSession(
            session_id="agent-run-21",
            messages=[],
            handoff_summary=None,
            handoff_message_count=0,
        )
    )
    await session.flush()

    summary = await latest_thread_handoff_summary(session, IDEA_ID)

    assert summary == {"found": False}


def _handoff_payload(active_objective: str, *, message_count: int) -> dict:
    return {
        "schema_version": 1,
        "source": "deterministic_fallback",
        "message_count": message_count,
        "previous_message_count": 0,
        "checkpoint": {
            "schema_version": 1,
            "source": "deterministic_fallback",
            "active_objective": active_objective,
            "user_constraints": ["Keep it read-only."],
            "completed_work": ["Found the Activity panel."],
            "current_plan": ["Expose the summary beside Activity."],
            "files_or_objects_touched": ["frontend/src/lib/features/threads/components/ThreadStageRightDock.svelte"],
            "decisions": [],
            "failed_attempts": [],
            "important_tool_results": [],
            "open_questions": [],
            "verification_status": "Not verified yet.",
            "recent_user_intent": active_objective,
            "risks_or_unknowns": [],
            "metadata": {},
        },
        "metadata": {"session_id": "agent-run-12-worker"},
        "digest": "digest",
    }


def _run(run_id: int, thread_id: str, created_at: datetime) -> AgentRunRow:
    return AgentRunRow(
        id=run_id,
        thread_id=thread_id,
        profile="fast",
        recipe="fast",
        status="completed",
        input_message="work",
        target_ref={},
        workspace_ref={},
        model_policy={},
        metadata_={},
        created_at=created_at,
        started_at=created_at,
        completed_at=created_at + timedelta(minutes=1),
    )


def _agent_session(
    session_id: str,
    handoff_summary: dict,
    *,
    handoff_updated_at: datetime,
) -> AgentSession:
    return AgentSession(
        session_id=session_id,
        messages=[],
        handoff_summary=handoff_summary,
        handoff_message_count=handoff_summary["message_count"],
        handoff_updated_at=handoff_updated_at,
        created_at=handoff_updated_at,
        updated_at=handoff_updated_at,
    )


def _register_sqlite_functions(dbapi_conn, connection_record):
    _ = connection_record
    dbapi_conn.create_function("NOW", 0, lambda: datetime.now(timezone.utc).isoformat())
    dbapi_conn.create_function("gen_random_uuid", 0, lambda: str(uuid.uuid4()))


def _patch_sqlite_for_pg_types() -> None:
    for name in ("visit_JSONB", "visit_ARRAY", "visit_UUID", "visit_VECTOR", "visit_Vector"):
        if not hasattr(SQLiteTypeCompiler, name):
            setattr(SQLiteTypeCompiler, name, lambda self, type_, **kw: "TEXT")
    SQLiteTypeCompiler.visit_BIGINT = lambda self, type_, **kw: "INTEGER"

    original = SQLiteDDLCompiler.get_column_default_string
    if getattr(original, "_thread_handoff_summary_patch", False):
        return

    def patched(self, column, **kw):
        result = original(self, column, **kw)
        if result:
            result = re.sub(r"::jsonb", "", result)
            result = result.replace("NOW()", "CURRENT_TIMESTAMP")
        return result

    patched._thread_handoff_summary_patch = True
    SQLiteDDLCompiler.get_column_default_string = patched
