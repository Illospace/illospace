"""Cortex thread context projected into AgentRun prompts."""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

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


async def test_status_question_context_tracks_live_then_completed_originating_run(session):
    from brain.systems.runs.status import RunStatus
    from brain.systems.runs.status_questions import build_status_question_context

    started_at = datetime(2026, 7, 22, 17, 37, 24, tzinfo=timezone.utc)
    origin = AgentRunRow(
        id=2327,
        org_id=ORG_ID,
        user_id=USER_ID,
        thread_id=IDEA_ID,
        profile="fast",
        recipe="fast",
        status="verifying",
        input_message="we may have a bug, email from a customer, assign ticket to me",
        target_ref={},
        workspace_ref={},
        model_policy={},
        metadata_={},
        created_at=started_at,
        started_at=started_at,
    )
    session.add(origin)
    await session.flush()

    live_context = await build_status_question_context(
        session,
        thread_id=IDEA_ID,
        org_id=ORG_ID,
        message="was it done?",
    )

    assert live_context is not None
    assert live_context["originating_run"]["run_id"] == 2327
    assert live_context["originating_run"]["status"] is RunStatus.VERIFYING
    assert live_context["live_sibling_runs"] == [
        {"run_id": 2327, "status": RunStatus.VERIFYING}
    ]
    assert [item["kind"] for item in live_context["deliverables"]] == [
        "github_issue",
        "assignment",
    ]

    origin.status = "completed"
    origin.completed_at = started_at + timedelta(minutes=18)
    session.add(
        AgentRunArtifactRow(
            run_id=2327,
            root_run_id=2327,
            artifact_type="final_answer",
            text=(
                "Created GitHub issue #1210, assigned it to Reda, and linked "
                "Customer Support record #2383."
            ),
            payload={},
            visibility="public",
            created_at=origin.completed_at,
        )
    )
    await session.flush()

    completed_context = await build_status_question_context(
        session,
        thread_id=IDEA_ID,
        org_id=ORG_ID,
        message="was it done?",
    )

    assert completed_context is not None
    assert completed_context["originating_run"]["status"] == "completed"
    assert completed_context["live_sibling_runs"] == []
    assert "GitHub issue #1210" in completed_context["originating_run"]["final_output"]
    assert "#2383" in completed_context["originating_run"]["final_output"]


async def test_slack_work_intake_attaches_same_thread_status_context(session):
    from brain.systems.runs.work_intake import WorkIntakeEvent, build_agent_run_request

    thread_ts = "1784741844.000100"
    slack_thread_id = f"slack:T_ALERTS:C_ALERTS:{thread_ts}"
    session.add(
        AgentRunRow(
            id=2328,
            org_id=ORG_ID,
            user_id=USER_ID,
            thread_id=slack_thread_id,
            profile="fast",
            recipe="fast",
            status="running",
            input_message=(
                "we may have a bug, email from a customer, assign ticket to me"
            ),
            target_ref={},
            workspace_ref={},
            model_policy={},
            metadata_={},
            created_at=datetime(2026, 7, 22, 17, 37, 24, tzinfo=timezone.utc),
        )
    )
    await session.flush()

    request = await build_agent_run_request(
        session,
        WorkIntakeEvent(
            source="slack",
            event_type="slack.message",
            org_id=ORG_ID,
            actor={"id": USER_ID, "org_id": ORG_ID, "internal": False},
            target={
                "kind": "slack_message",
                "team_id": "T_ALERTS",
                "channel_id": "C_ALERTS",
                "thread_ts": thread_ts,
            },
            payload={
                "message": "was it done?",
                "metadata": {
                    "slack_trigger": {
                        "team_id": "T_ALERTS",
                        "channel_id": "C_ALERTS",
                        "thread_ts": thread_ts,
                    }
                },
            },
        ),
    )

    status_context = request.metadata["same_thread_run_context"]
    assert request.thread_id == slack_thread_id
    assert status_context["originating_run"]["run_id"] == 2328
    assert status_context["originating_run"]["status"] == "running"
    assert status_context["live_sibling_runs"] == [
        {"run_id": 2328, "status": "running"}
    ]


async def test_slack_work_intake_attaches_active_sibling_to_non_status_question(session):
    from brain.systems.runs.context import RunContextLoader
    from brain.systems.runs.work_intake import WorkIntakeEvent, build_agent_run_request

    thread_ts = "1785870481.000100"
    slack_thread_id = f"slack:T:C:{thread_ts}"
    session.add(
        AgentRunRow(
            id=14644,
            org_id=ORG_ID,
            user_id=USER_ID,
            thread_id=slack_thread_id,
            root_run_id=14644,
            profile="fast",
            recipe="fast",
            status="running",
            input_message="Investigate the customer's generation payloads.",
            target_ref={},
            workspace_ref={},
            model_policy={},
            metadata_={},
            created_at=datetime(2026, 8, 4, 18, 51, 53, tzinfo=timezone.utc),
        )
    )
    await session.flush()
    session.add(
        AgentRunRow(
            id=14646,
            org_id=ORG_ID,
            user_id=USER_ID,
            thread_id=slack_thread_id,
            parent_run_id=14644,
            root_run_id=14644,
            parent_step_key_hash="payload-batch-step",
            profile="fast",
            recipe="fast",
            status="running",
            input_message="Inspect one payload batch.",
            target_ref={},
            workspace_ref={},
            model_policy={},
            metadata_={},
            created_at=datetime(2026, 8, 4, 18, 52, 10, tzinfo=timezone.utc),
        )
    )
    await session.flush()

    request = await build_agent_run_request(
        session,
        WorkIntakeEvent(
            source="slack",
            event_type="slack.message",
            org_id=ORG_ID,
            actor={"id": USER_ID, "org_id": ORG_ID, "internal": False},
            target={
                "kind": "slack_message",
                "team_id": "T",
                "channel_id": "C",
                "thread_ts": thread_ts,
            },
            payload={
                "message": "Which image model is she trying to use?",
                "metadata": {
                    "slack_trigger": {
                        "team_id": "T",
                        "channel_id": "C",
                        "thread_ts": thread_ts,
                    }
                },
            },
        ),
    )

    assert request.thread_id == slack_thread_id
    assert request.metadata["same_thread_run_context"]["live_sibling_runs"] == [
        {"run_id": 14644, "status": "running"}
    ]
    assert all(
        item["run_id"] != 14646
        for item in request.metadata["same_thread_run_context"]["live_sibling_runs"]
    )

    context = RunContextLoader().load(
        thread_id=request.thread_id,
        message=request.message,
        target_ref=request.target_ref,
        metadata=request.metadata,
    ).prompt_context()
    assert "Authoritative active-sibling evidence" in context
    assert "Sibling run 14644 status: running" in context
    assert "wait for the active work" in context
    assert "coordination field" in context


async def test_slack_work_intake_without_active_sibling_has_no_deferral_context(session):
    from brain.systems.runs.work_intake import WorkIntakeEvent, build_agent_run_request

    thread_ts = "1785870481.000200"
    request = await build_agent_run_request(
        session,
        WorkIntakeEvent(
            source="slack",
            event_type="slack.message",
            org_id=ORG_ID,
            actor={"id": USER_ID, "org_id": ORG_ID, "internal": False},
            target={
                "kind": "slack_message",
                "team_id": "T",
                "channel_id": "C",
                "thread_ts": thread_ts,
            },
            payload={
                "message": "Which image model is she trying to use?",
                "metadata": {
                    "slack_trigger": {
                        "team_id": "T",
                        "channel_id": "C",
                        "thread_ts": thread_ts,
                    }
                },
            },
        ),
    )

    assert "same_thread_run_context" not in request.metadata


def test_interactive_slack_classifier_uses_surface_policy_for_monitors():
    from brain.systems.runs.interactive_reply import is_interactive_slack_reply_context

    assert not is_interactive_slack_reply_context(
        {
            "origin": "slack_channel_monitor",
            "headless": True,
            "final_answer_target_surface": "headless",
        }
    )
    assert is_interactive_slack_reply_context(
        {
            "origin": "slack_channel_monitor",
            "headless": False,
            "final_answer_target_surface": "slack",
        }
    )


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


def test_run_context_prompt_marks_live_status_work_as_in_progress():
    from brain.systems.runs.context import RunContextLoader

    context = RunContextLoader().load(
        thread_id="idea-1",
        message="was it done?",
        metadata={
            "same_thread_run_context": {
                "thread_id": "idea-1",
                "lookup_status": "verified",
                "status_question": True,
                "originating_run": {
                    "run_id": 2327,
                    "status": "running",
                    "request": "assign ticket to me",
                },
                "live_sibling_runs": [{"run_id": 2327, "status": "running"}],
                "deliverables": [
                    {"kind": "github_issue", "label": "GitHub ticket"},
                    {"kind": "assignment", "label": "ticket assignment"},
                ],
            }
        },
    )

    prompt_context = context.prompt_context()

    assert "Authoritative status-check evidence" in prompt_context
    assert "run 2327 is running" in prompt_context
    assert "must report the request as in progress" in prompt_context
    assert "GitHub ticket" in prompt_context
    assert "ticket assignment" in prompt_context


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
