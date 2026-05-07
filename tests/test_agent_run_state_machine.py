"""Restart-safety tests for the AgentRun state machine."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.sqlite.base import SQLiteDDLCompiler, SQLiteTypeCompiler
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from brain.systems.runs.domain import AgentRunRequest, RunRecipe
from brain.systems.runs.engine import AgentRunEngine, RunRecipeResult, RunRuntime, StaticAnswerRecipe
from brain.systems.runs.status import RunStatus
from brain.systems.runs.store import AgentRunStore
from brain.platform.db.models.agent_run import AgentRunArtifactRow, AgentRunEventRow, AgentRunRow


@pytest.fixture
def session_factory() -> Iterator[Callable[[], Session]]:
    _patch_sqlite_for_agent_run_tables()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    for table in [
        AgentRunRow.__table__,
        AgentRunEventRow.__table__,
        AgentRunArtifactRow.__table__,
    ]:
        table.create(engine, checkfirst=True)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


def test_restart_resume_skips_completed_steps_from_persisted_cursor(session_factory):
    calls: list[str] = []

    class TwoStepRecipe:
        def execute(self, runtime: RunRuntime) -> RunRecipeResult:
            first = runtime.step("first", lambda: calls.append("first") or "first-result")
            second = runtime.step("second", lambda: calls.append("second") or "second-result")
            return RunRecipeResult(output=f"{first}/{second}")

    session = session_factory()
    store = AgentRunStore(session)
    run = store.create_run(AgentRunRequest(thread_id="thread-1", message="resume me"))
    store.set_status(run.id, RunStatus.STARTING)
    store.set_status(run.id, RunStatus.RUNNING)
    store.start_step(run.id, "first")
    store.complete_step(run.id, "first", "first-result")
    session.commit()
    session.close()

    restarted = session_factory()
    result = AgentRunEngine(restarted, recipes={"fast": TwoStepRecipe()}).resume(run.id)
    restarted.commit()

    assert result.status == RunStatus.COMPLETED
    assert calls == ["second"]
    row = restarted.get(AgentRunRow, run.id)
    assert row is not None
    assert row.metadata_["cursor"]["completed_steps"]["first"]["result"] == "first-result"
    assert row.metadata_["cursor"]["completed_steps"]["second"]["result"] == "second-result"
    assert _event_types(restarted, run.id).count("run.step_skipped") == 1


def test_claim_and_completion_are_idempotent(session_factory):
    session = session_factory()
    store = AgentRunStore(session)
    first = store.create_run(AgentRunRequest(thread_id="thread-1", message="one"))
    second = store.create_run(AgentRunRequest(thread_id="thread-1", message="two"))

    claimed = store.claim_next()
    assert claimed is not None
    assert claimed.id == first.id
    assert claimed.status == RunStatus.STARTING
    assert store.claim_run(first.id) is None
    assert store.claim_next().id == second.id

    engine = AgentRunEngine(session, recipes={"fast": StaticAnswerRecipe("done")})
    completed = engine.run_existing(first.id)
    assert completed.status == RunStatus.COMPLETED
    engine.complete(first.id, output="done again")

    events = _event_types(session, first.id)
    assert events.count("run.completed") == 1


def test_deferred_queued_run_waits_for_target_terminal(session_factory):
    session = session_factory()
    store = AgentRunStore(session)
    active = store.create_run(AgentRunRequest(thread_id="thread-1", message="active"))
    store.set_status(active.id, RunStatus.STARTING)
    store.set_status(active.id, RunStatus.RUNNING)
    deferred = store.create_run(
        AgentRunRequest(
            thread_id="thread-1",
            message="queued next",
            metadata={"queued_after_run_id": active.id},
        )
    )

    assert store.claim_next() is None

    store.set_status(active.id, RunStatus.COMPLETED)

    claimed = store.claim_next()
    assert claimed is not None
    assert claimed.id == deferred.id
    assert claimed.status == RunStatus.STARTING


def test_deferred_queued_runs_stay_sequential_after_target_finishes(session_factory):
    session = session_factory()
    store = AgentRunStore(session)
    active = store.create_run(AgentRunRequest(thread_id="thread-1", message="active"))
    store.set_status(active.id, RunStatus.STARTING)
    store.set_status(active.id, RunStatus.RUNNING)
    first_deferred = store.create_run(
        AgentRunRequest(
            thread_id="thread-1",
            message="queued first",
            metadata={"queued_after_run_id": active.id},
        )
    )
    second_deferred = store.create_run(
        AgentRunRequest(
            thread_id="thread-1",
            message="queued second",
            metadata={"queued_after_run_id": active.id},
        )
    )

    store.set_status(active.id, RunStatus.COMPLETED)

    first_claimed = store.claim_next()
    assert first_claimed is not None
    assert first_claimed.id == first_deferred.id
    assert store.claim_next() is None

    store.set_status(first_deferred.id, RunStatus.RUNNING)
    store.set_status(first_deferred.id, RunStatus.COMPLETED)

    second_claimed = store.claim_next()
    assert second_claimed is not None
    assert second_claimed.id == second_deferred.id


def test_blocked_deferred_run_does_not_starve_other_threads(session_factory):
    session = session_factory()
    store = AgentRunStore(session)
    active = store.create_run(AgentRunRequest(thread_id="thread-1", message="active"))
    store.set_status(active.id, RunStatus.STARTING)
    store.set_status(active.id, RunStatus.RUNNING)
    for index in range(30):
        store.create_run(
            AgentRunRequest(
                thread_id="thread-1",
                message=f"queued next {index}",
                metadata={"queued_after_run_id": active.id},
            )
        )
    eligible = store.create_run(AgentRunRequest(thread_id="thread-2", message="normal queued"))

    claimed = store.claim_next()
    assert claimed is not None
    assert claimed.id == eligible.id


def test_durable_steering_drains_once_from_run_events(session_factory):
    session = session_factory()
    store = AgentRunStore(session)
    run = store.create_run(AgentRunRequest(thread_id="thread-1", message="listen"))
    event = store.append_steering(
        run.id,
        "  Don't fetch everything.  ",
        user_id="user-1",
        thread_message_id=7,
    )

    first = store.drain_steering(run.id)
    second = store.drain_steering(run.id)

    assert [message.content for message in first] == ["Don't fetch everything."]
    assert first[0].user_id == "user-1"
    assert second == []
    row = session.get(AgentRunRow, run.id)
    assert row is not None
    assert row.metadata_["steering_cursor_sequence_no"] == event.sequence_no
    event_payload = session.get(AgentRunEventRow, event.id).payload
    assert event_payload["thread_message_id"] == 7


def test_durable_steering_no_rows_autocommit_releases_transaction(session_factory, monkeypatch):
    session = session_factory()
    store = AgentRunStore(session, auto_commit=True)
    run = store.create_run(AgentRunRequest(thread_id="thread-1", message="listen"))

    def fail_locked_run(_run_id):
        raise AssertionError("draining an empty steering inbox should not lock the run")

    monkeypatch.setattr(store, "_locked_run", fail_locked_run)

    assert store.drain_steering(run.id) == []
    assert not session.in_transaction()


def test_durable_steering_with_rows_autocommit_releases_transaction(session_factory):
    session = session_factory()
    store = AgentRunStore(session, auto_commit=True)
    run = store.create_run(AgentRunRequest(thread_id="thread-1", message="listen"))
    event = store.append_steering(run.id, "keep going", user_id="user-1")

    messages = store.drain_steering(run.id)

    assert [message.content for message in messages] == ["keep going"]
    assert messages[0].user_id == "user-1"
    assert not session.in_transaction()
    row = session.get(AgentRunRow, run.id)
    assert row is not None
    assert row.metadata_["steering_cursor_sequence_no"] == event.sequence_no


def test_run_heartbeat_updates_liveness_without_event_noise(session_factory):
    session = session_factory()
    store = AgentRunStore(session)
    run = store.create_run(AgentRunRequest(thread_id="thread-1", message="heartbeat"))
    store.set_status(run.id, RunStatus.STARTING)
    before_events = _event_types(session, run.id)
    now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)

    assert store.heartbeat_run(run.id, token="runner-1", reason="runner_running", now=now)

    row = session.get(AgentRunRow, run.id)
    assert row is not None
    assert row.metadata_["runner_heartbeat"]["token"] == "runner-1"
    assert row.metadata_["runner_heartbeat"]["reason"] == "runner_running"
    assert row.metadata_["runner_heartbeat"]["at"] == now.isoformat()
    assert _event_types(session, run.id) == before_events


def test_failed_recipe_records_step_and_run_failure(session_factory):
    class FailingRecipe:
        def execute(self, runtime: RunRuntime) -> RunRecipeResult:
            runtime.step("explode", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
            return RunRecipeResult(output="unreachable")

    session = session_factory()
    result = AgentRunEngine(session, recipes={"fast": FailingRecipe()}).run(
        AgentRunRequest(thread_id="thread-1", message="fail")
    )

    assert result.status == RunStatus.FAILED
    events = _event_types(session, result.id)
    assert "run.step_failed" in events
    assert "run.failed" in events
    row = session.get(AgentRunRow, result.id)
    assert row is not None
    assert row.metadata_["last_failed_step"] == "explode"


def test_runtime_cancel_token_stops_run_after_recipe_returns(session_factory):
    class SlowRecipe:
        def execute(self, runtime: RunRuntime) -> RunRecipeResult:
            assert runtime.cancel_event is not None
            return RunRecipeResult(output="should not complete")

    session = session_factory()
    result = AgentRunEngine(
        session,
        recipes={"fast": SlowRecipe()},
        cancel_event_factory=lambda _run_id: SimpleNamespace(is_set=lambda: True),
    ).run(AgentRunRequest(thread_id="thread-1", message="cancel me"))

    assert result.status == RunStatus.CANCELED
    events = _event_types(session, result.id)
    assert "run.canceled" in events
    assert "run.completed" not in events


def test_auto_commit_store_finishes_event_transactions(session_factory):
    session = session_factory()
    store = AgentRunStore(session, auto_commit=True)

    with patch.object(session, "commit", wraps=session.commit) as commit:
        run = store.create_run(AgentRunRequest(thread_id="thread-1", message="live"))
        store.set_status(run.id, RunStatus.STARTING)

    assert commit.call_count >= 2


def test_cancel_endpoint_helper_records_run_canceled_event(session_factory):
    from brain.app.api.routers.cortex._run import _cancel_run_with_event

    session = session_factory()
    store = AgentRunStore(session)
    run = store.create_run(AgentRunRequest(thread_id="thread-1", message="cancel me"))

    _cancel_run_with_event(store, run.id, reason="user_canceled")

    row = session.get(AgentRunRow, run.id)
    assert row is not None
    assert row.status == RunStatus.CANCELED.value
    events = _event_types(session, run.id)
    assert "run.canceled" in events
    assert events.count("run.canceled") == 1


def test_cancel_runs_for_idea_records_run_canceled_event(session_factory):
    from brain.systems.runs.cortex import cancel_runs_for_idea

    session = session_factory()
    store = AgentRunStore(session)
    run = store.create_run(AgentRunRequest(thread_id="idea-1", message="cancel me"))
    store.set_status(run.id, RunStatus.STARTING)
    store.set_status(run.id, RunStatus.RUNNING)

    class _UoW:
        def __enter__(self):
            self.session = session
            return self

        def __exit__(self, exc_type, exc, tb):
            if exc_type is None:
                session.flush()
            return False

    with patch("brain.systems.runs.cortex.UnitOfWork", return_value=_UoW()):
        count = cancel_runs_for_idea("idea-1")

    row = session.get(AgentRunRow, run.id)
    assert count == 1
    assert row is not None
    assert row.status == RunStatus.CANCELED.value
    events = _event_types(session, run.id)
    assert "run.canceled" in events
    assert events.count("run.canceled") == 1


def test_stale_active_run_reaper_fails_abandoned_runs(session_factory):
    from brain.systems.runs.cortex import runner

    session = session_factory()
    store = AgentRunStore(session)
    run = store.create_run(AgentRunRequest(thread_id="idea-1", message="stale"))
    store.set_status(run.id, RunStatus.STARTING)
    store.set_status(run.id, RunStatus.RUNNING)
    now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
    old = now - timedelta(seconds=300)
    row = session.get(AgentRunRow, run.id)
    assert row is not None
    row.created_at = old
    row.started_at = old
    row.updated_at = old
    row.metadata_ = {"runner_heartbeat": {"at": old.isoformat(), "reason": "runner_running"}}
    for event in session.scalars(select(AgentRunEventRow).where(AgentRunEventRow.run_id == run.id)):
        event.created_at = old
    session.commit()

    class _UoW:
        def __enter__(self):
            self.session = session
            return self

        def __exit__(self, exc_type, exc, tb):
            if exc_type is None:
                session.flush()
            return False

    live_events: list[tuple[str, dict]] = []
    with (
        patch("brain.systems.runs.cortex.runner.UnitOfWork", return_value=_UoW()),
        patch(
            "brain.systems.runs.cortex.runner.publish_live_safe",
            lambda event, payload: live_events.append((event, payload)),
        ),
        patch("brain.systems.runs.cortex.runner.publish_safe"),
        patch("brain.systems.runs.cortex.runner._settle_idea_for_terminal_root_run", return_value=None),
    ):
        count = runner.reap_stale_active_runs(now=now, stale_after_seconds=120)

    row = session.get(AgentRunRow, run.id)
    assert count == 1
    assert row is not None
    assert row.status == RunStatus.FAILED.value
    failed_events = [
        event
        for event in session.scalars(
            select(AgentRunEventRow)
            .where(AgentRunEventRow.run_id == run.id, AgentRunEventRow.event_type == "run.failed")
            .order_by(AgentRunEventRow.sequence_no.asc())
        )
    ]
    assert len(failed_events) == 1
    assert failed_events[0].payload["reason"] == "runner_heartbeat_stale"
    assert live_events and live_events[-1][0] == "run_completed"


def test_stale_active_run_reaper_uses_recent_events_as_liveness(session_factory):
    from brain.systems.runs.events import run_event
    from brain.systems.runs.cortex import runner

    session = session_factory()
    store = AgentRunStore(session)
    run = store.create_run(AgentRunRequest(thread_id="idea-1", message="active events"))
    store.set_status(run.id, RunStatus.STARTING)
    store.set_status(run.id, RunStatus.RUNNING)
    now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
    old = now - timedelta(seconds=300)
    recent = now - timedelta(seconds=30)
    row = session.get(AgentRunRow, run.id)
    assert row is not None
    row.created_at = old
    row.started_at = old
    row.updated_at = old
    row.metadata_ = {"runner_heartbeat": {"at": old.isoformat(), "reason": "runner_running"}}
    for event in session.scalars(select(AgentRunEventRow).where(AgentRunEventRow.run_id == run.id)):
        event.created_at = old
    recent_event = store.append_event(run_event(run.id, "run.tool_completed", {"tool": "read_file"}))
    recent_event.created_at = recent
    session.commit()

    class _UoW:
        def __enter__(self):
            self.session = session
            return self

        def __exit__(self, exc_type, exc, tb):
            if exc_type is None:
                session.flush()
            return False

    with (
        patch("brain.systems.runs.cortex.runner.UnitOfWork", return_value=_UoW()),
        patch("brain.systems.runs.cortex.runner.publish_safe"),
        patch("brain.systems.runs.cortex.runner._settle_idea_for_terminal_root_run", return_value=None),
    ):
        count = runner.reap_stale_active_runs(now=now, stale_after_seconds=120)

    row = session.get(AgentRunRow, run.id)
    assert count == 0
    assert row is not None
    assert row.status == RunStatus.RUNNING.value


def test_stale_active_run_reaper_does_not_reap_child_while_root_active(session_factory):
    from brain.systems.runs.cortex import runner

    session = session_factory()
    store = AgentRunStore(session)
    root = store.create_run(AgentRunRequest(thread_id="idea-1", message="root"))
    child = store.create_child_run(root, recipe=RunRecipe.WORKER, message="child", step_key="node:investigate")
    store.set_status(root.id, RunStatus.STARTING)
    store.set_status(root.id, RunStatus.RUNNING)
    store.set_status(child.id, RunStatus.STARTING)
    store.set_status(child.id, RunStatus.RUNNING)
    now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
    old = now - timedelta(seconds=300)
    root_row = session.get(AgentRunRow, root.id)
    child_row = session.get(AgentRunRow, child.id)
    assert root_row is not None
    assert child_row is not None
    root_row.updated_at = now
    child_row.created_at = old
    child_row.started_at = old
    child_row.updated_at = old
    child_row.metadata_ = {"runner_heartbeat": {"at": old.isoformat(), "reason": "runner_started"}}
    for event in session.scalars(select(AgentRunEventRow).where(AgentRunEventRow.run_id == child.id)):
        event.created_at = old
    session.commit()

    class _UoW:
        def __enter__(self):
            self.session = session
            return self

        def __exit__(self, exc_type, exc, tb):
            if exc_type is None:
                session.flush()
            return False

    with (
        patch("brain.systems.runs.cortex.runner.UnitOfWork", return_value=_UoW()),
        patch("brain.systems.runs.cortex.runner.publish_safe"),
        patch("brain.systems.runs.cortex.runner._settle_idea_for_terminal_root_run", return_value=None),
    ):
        count = runner.reap_stale_active_runs(now=now, stale_after_seconds=120)

    child_row = session.get(AgentRunRow, child.id)
    assert count == 0
    assert child_row is not None
    assert child_row.status == RunStatus.RUNNING.value


def _event_types(session: Session, run_id: int) -> list[str]:
    return list(
        session.scalars(
            select(AgentRunEventRow.event_type)
            .where(AgentRunEventRow.run_id == int(run_id))
            .order_by(AgentRunEventRow.sequence_no.asc())
        )
    )


def _patch_sqlite_for_agent_run_tables() -> None:
    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "TEXT"
    SQLiteTypeCompiler.visit_BIGINT = lambda self, type_, **kw: "INTEGER"
    original = SQLiteDDLCompiler.get_column_default_string

    if getattr(original, "_agent_run_patch", False):
        return

    def patched(self, column, **kw):
        result = original(self, column, **kw)
        if result:
            result = re.sub(r"::jsonb", "", result)
            result = result.replace("NOW()", "CURRENT_TIMESTAMP")
        return result

    patched._agent_run_patch = True
    SQLiteDDLCompiler.get_column_default_string = patched
