"""Cortex thread context projected into AgentRun prompts."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects.sqlite.base import SQLiteDDLCompiler, SQLiteTypeCompiler

from brain.platform.db.models.agent_run import AgentRunArtifactRow, AgentRunRow
from brain.platform.db.models.idea import Idea, IdeaThread
from brain.platform.db.models.org import Org, User

ORG_ID = "11111111-1111-1111-1111-111111111111"
USER_ID = "22222222-2222-2222-2222-222222222222"
IDEA_ID = "33333333-3333-3333-3333-333333333333"


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
            IdeaThread.__table__,
            AgentRunRow.__table__,
            AgentRunArtifactRow.__table__,
        ],
        connect_listener=_register_sqlite_functions,
    )
    org = Org(id=ORG_ID, name="Example", slug="example")
    user = User(id=USER_ID, org_id=ORG_ID, name="Alex", email="alex@example.test")
    idea = Idea(id=IDEA_ID, title="Example API skill", user_id=USER_ID, org_id=ORG_ID)
    db.add_all([org, user, idea])
    await db.flush()
    return db


async def test_agent_visible_context_matches_prior_visible_thread(session):
    from brain.systems.cortex.thread_context import async_build_agent_visible_thread_context

    now = datetime(2026, 5, 6, 18, 0, tzinfo=timezone.utc)
    session.add(
        IdeaThread(
            id=1,
            idea_id=IDEA_ID,
            role="user",
            content="Here is the EXAMPLE reference. Can you make a skill?",
            user_id=USER_ID,
            created_at=now,
        )
    )
    await _add_run_final_answer(
        session,
        run_id=7,
        text="Yep, I created the use-example-api skill. Name the secret EXAMPLE_API_KEY.",
        created_at=now + timedelta(seconds=20),
    )
    session.add(
        IdeaThread(
            id=2,
            idea_id=IDEA_ID,
            role="user",
            content="fantastic I added it do you see it now?",
            user_id=USER_ID,
            created_at=now + timedelta(seconds=40),
        )
    )
    await session.flush()

    context = await async_build_agent_visible_thread_context(
        session,
        IDEA_ID,
        current_thread_message_id=2,
        current_message="fantastic I added it do you see it now?",
    )

    assert context is not None
    assert "Here is the EXAMPLE reference" in context["formatted"]
    assert "use-example-api" in context["formatted"]
    assert "EXAMPLE_API_KEY" in context["formatted"]
    assert "fantastic I added it" not in context["formatted"]
    assert [message["role"] for message in context["messages"]] == ["user", "illo"]


async def test_agent_visible_context_keeps_prior_thread_attachments(session, tmp_path, monkeypatch):
    from brain.systems.cortex import thread_attachments
    from brain.systems.cortex.thread_context import async_build_agent_visible_thread_context

    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    screenshot = upload_dir / "screenshot.png"
    screenshot.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    monkeypatch.setattr(thread_attachments, "UPLOAD_DIR", upload_dir)

    now = datetime(2026, 5, 6, 18, 0, tzinfo=timezone.utc)
    session.add(
        IdeaThread(
            id=21,
            idea_id=IDEA_ID,
            role="user",
            content="you mean this?",
            attachments=[
                {"url": "/static/uploads/screenshot.png", "filename": "screenshot.png", "type": "image/png"}
            ],
            user_id=USER_ID,
            created_at=now,
        )
    )
    session.add(
        IdeaThread(
            id=22,
            idea_id=IDEA_ID,
            role="user",
            content="can you see my screenshot?",
            user_id=USER_ID,
            created_at=now + timedelta(seconds=30),
        )
    )
    await session.flush()

    context = await async_build_agent_visible_thread_context(
        session,
        IDEA_ID,
        current_thread_message_id=22,
        current_message="can you see my screenshot?",
    )

    assert context is not None
    assert "you mean this? [Attachments: screenshot.png (image)]" in context["formatted"]
    assert "can you see my screenshot?" not in context["formatted"]
    assert context["messages"][0]["attachments_count"] == 1
    attachment_context = context["thread_attachment_context"]
    assert attachment_context["source"] == "cortex-visible-thread-attachments"
    assert attachment_context["items"][0]["kind"] == "image"
    assert attachment_context["items"][0]["filename"] == "screenshot.png"
    assert "Earlier Thread Attachments" in attachment_context["prompt"]


async def test_work_intake_attaches_prior_visible_context_without_current_message(session):
    from brain.systems.runs.work_intake import WorkIntakeEvent, build_agent_run_request

    now = datetime(2026, 5, 6, 18, 0, tzinfo=timezone.utc)
    session.add(
        IdeaThread(
            id=11,
            idea_id=IDEA_ID,
            role="user",
            content="Create a Example API skill from this reference.",
            user_id=USER_ID,
            created_at=now,
        )
    )
    await _add_run_final_answer(
        session,
        run_id=12,
        text="Done. The skill is use-example-api and the key should be EXAMPLE_API_KEY.",
        created_at=now + timedelta(seconds=15),
    )
    session.add(
        IdeaThread(
            id=13,
            idea_id=IDEA_ID,
            role="user",
            content="fantastic I added it do you see it now?",
            user_id=USER_ID,
            created_at=now + timedelta(seconds=30),
        )
    )
    await session.flush()

    request = await build_agent_run_request(
        session,
        WorkIntakeEvent(
            source="cortex",
            event_type="cortex.thread_reply",
            org_id=ORG_ID,
            actor={"id": USER_ID, "org_id": ORG_ID, "internal": False},
            target={"kind": "cortex_idea", "idea_id": IDEA_ID},
            payload={
                "message": "fantastic I added it do you see it now?",
                "metadata": {"run_profile": "fast", "thread_message_id": 13},
            },
            policy={"run_event": "thread_reply"},
        ),
    )

    assert request.message == "fantastic I added it do you see it now?"
    thread_context = request.metadata["thread_context"]
    assert "Create a Example API skill" in thread_context["formatted"]
    assert "use-example-api" in thread_context["formatted"]
    assert "EXAMPLE_API_KEY" in thread_context["formatted"]
    assert "fantastic I added it" not in thread_context["formatted"]


def test_fast_recipe_uses_prior_thread_attachment_context_when_current_message_has_none():
    from brain.systems.runs.recipes.fast import _thread_attachment_context

    prior_context = {"source": "cortex-visible-thread-attachments", "items": [{"kind": "image"}]}
    runtime = SimpleNamespace(
        request=SimpleNamespace(
            metadata={"thread_context": {"thread_attachment_context": prior_context}},
            target_ref={},
            workspace_ref={},
        )
    )

    assert _thread_attachment_context(runtime) == prior_context


def test_run_context_prompt_includes_thread_context():
    from brain.systems.runs.context import RunContextLoader

    context = RunContextLoader().load(
        thread_id="idea-1",
        message="Can you see it?",
        metadata={
            "thread_context": {
                "formatted": "User: earlier request\nIllo: earlier answer",
            }
        },
    )

    prompt_context = context.prompt_context()
    assert "Thread so far, before the current user message:" in prompt_context
    assert "Illo: earlier answer" in prompt_context


def test_run_context_prompt_compacts_large_project_context_references():
    from brain.systems.runs.context import RunContextLoader

    huge_value = "x" * 2_000_000
    project_ref = {
        "kind": "cortex_idea",
        "title": "Port the SEO workflow",
        "project_context_snapshot": {
            "name": "Agent Mission Control Reference",
            "resources": [
                {
                    "kind": "folder",
                    "path": "/workspaces/agent-mission-control-reference",
                    "materialization": {
                        "status": "ready",
                        "project_root_file_count": 779,
                        "imports": {
                            "imported": [huge_value],
                            "root_versions": {"before": huge_value},
                            "project_root_file_count": 779,
                        },
                    },
                }
            ],
        },
    }

    context = RunContextLoader().load(
        thread_id="idea-1",
        message="Can you see it?",
        target_ref=project_ref,
        workspace_ref=project_ref,
    )

    prompt_context = context.prompt_context()

    assert len(prompt_context) < 30_000
    assert huge_value[:1000] not in prompt_context
    assert "large value omitted from prompt context" in prompt_context
    assert '"project_root_file_count": 779' in prompt_context
    assert "/workspaces/agent-mission-control-reference" in prompt_context


def test_run_context_project_reference_compaction_keeps_non_heavy_content_fields():
    from brain.systems.runs.context import RunContextLoader

    context = RunContextLoader().load(
        thread_id="idea-1",
        message="Can you see it?",
        target_ref={
            "kind": "cortex_idea",
            "content": "This is a useful lightweight content field.",
        },
    )

    prompt_context = context.prompt_context()

    assert "This is a useful lightweight content field." in prompt_context
    assert "large value omitted from prompt context" not in prompt_context


def test_thread_context_formatting_prefers_recent_entries_when_budget_is_tight():
    from brain.systems.cortex.thread_context import ThreadContextEntry, _format_entries

    now = datetime(2026, 5, 6, 18, 0, tzinfo=timezone.utc)
    entries = [
        ThreadContextEntry(
            role="user",
            content="old " * 200,
            created_at=now,
            thread_message_id=1,
        ),
        ThreadContextEntry(
            role="illo",
            content="new CRM exists",
            created_at=now + timedelta(minutes=1),
            artifact_id=2,
        ),
        ThreadContextEntry(
            role="user",
            content="newest question about drift",
            created_at=now + timedelta(minutes=2),
            thread_message_id=3,
        ),
    ]

    formatted, kept = _format_entries(entries, char_limit=120)

    assert "old old" not in formatted
    assert "new CRM exists" in formatted
    assert "newest question about drift" in formatted
    assert [entry.thread_message_id or entry.artifact_id for entry in kept] == [2, 3]


async def _add_run_final_answer(
    session,
    *,
    run_id: int,
    text: str,
    created_at: datetime,
) -> None:
    session.add(
        AgentRunRow(
            id=run_id,
            org_id=ORG_ID,
            user_id=USER_ID,
            thread_id=IDEA_ID,
            profile="fast",
            recipe="fast",
            status="completed",
            input_message="previous request",
            target_ref={},
            workspace_ref={},
            model_policy={},
            metadata_={},
            created_at=created_at - timedelta(seconds=10),
            started_at=created_at - timedelta(seconds=8),
            completed_at=created_at,
        )
    )
    await session.flush()
    session.add(
        AgentRunArtifactRow(
            run_id=run_id,
            root_run_id=run_id,
            artifact_type="final_answer",
            text=text,
            payload={},
            visibility="public",
            created_at=created_at,
        )
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
    if getattr(original, "_cortex_thread_context_patch", False):
        return

    def patched(self, column, **kw):
        result = original(self, column, **kw)
        if result:
            result = re.sub(r"::jsonb", "", result)
            result = result.replace("NOW()", "CURRENT_TIMESTAMP")
        return result

    patched._cortex_thread_context_patch = True
    SQLiteDDLCompiler.get_column_default_string = patched
