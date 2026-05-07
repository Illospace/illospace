from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from brain.app.api.schemas.cycles import CycleRead, CycleRunRead
from brain.systems.cycles import service
from brain.app.api.routers import cycles as cycles_router
from brain.platform.db.models.cycle import Cycle, CycleRun
from brain.platform.db.models.run import AgentRun
from brain.platform.db.models.idea import Idea


class _FakeSession:
    def __init__(self, agent_run=None, run=None, cycle=None):
        self._agent_run = agent_run
        self._run = run
        self._cycle = cycle

    def get(self, model, value):
        if model is AgentRun:
            return self._agent_run
        if model is CycleRun:
            return self._run
        if model is Cycle:
            return self._cycle
        return None


class _FakeUoW:
    def __init__(self, agent_run=None, run=None, cycle=None):
        self.session = _FakeSession(agent_run=agent_run, run=run, cycle=cycle)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def first(self):
        return self._value


class _FirstResult:
    def __init__(self, value=None):
        self._value = value

    def first(self):
        return self._value


class _ExecuteCycleSession:
    def __init__(self, *, run, cycle, idea, owner=None):
        self._scalar_values = [run, cycle, idea]
        self._owner = owner
        self.added = []

    def scalars(self, statement):
        return _ScalarResult(self._scalar_values.pop(0))

    def execute(self, statement):
        return _FirstResult(None)

    def get(self, model, value):
        return self._owner

    def add(self, value):
        self.added.append(value)

    def flush(self):
        for value in self.added:
            if value.__class__.__name__ == "IdeaThread" and getattr(value, "id", None) is None:
                value.id = 123


class _ExecuteCycleUoW:
    def __init__(self, session):
        self.session = session

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_compute_next_run_at_uses_timezone():
    baseline = datetime(2026, 4, 22, 12, 34, tzinfo=timezone.utc)
    next_run = service.compute_next_run_at(
        "0 9 * * *",
        "America/Toronto",
        from_dt=baseline,
    )
    assert next_run == datetime(2026, 4, 22, 13, 0, tzinfo=timezone.utc)


def test_cycle_defaults_reuse_same_idea_reopens_by_default():
    assert service.cycle_defaults(execution_mode="reuse_same_idea", reopen_archived=None) is True
    assert service.cycle_defaults(execution_mode="new_idea_per_run", reopen_archived=None) is True
    assert service.cycle_defaults(execution_mode="new_idea_per_run", reopen_archived=True) is True
    assert service.cycle_defaults(execution_mode="reuse_same_idea", reopen_archived=False) is True


def test_validate_execution_mode_coerces_legacy_new_thread_mode():
    assert service.validate_execution_mode(None) == service.REUSABLE_THREAD_EXECUTION_MODE
    assert service.validate_execution_mode("reuse_same_idea") == service.REUSABLE_THREAD_EXECUTION_MODE
    assert service.validate_execution_mode("new_idea_per_run") == service.REUSABLE_THREAD_EXECUTION_MODE


def test_validate_schedule_expr_rejects_non_five_field_expr():
    with pytest.raises(ValueError):
        service.validate_schedule_expr("0 0 9 * * *")


def test_validate_nonempty_trimmed_rejects_blank_text():
    with pytest.raises(ValueError):
        service.validate_nonempty_trimmed("   ", "name")


def _cycle_for_serialization(*, schedule_expr: str, timezone_name: str) -> Cycle:
    now = datetime(2026, 4, 27, 12, 0, tzinfo=timezone.utc)
    cycle = Cycle()
    cycle.id = 42
    cycle.user_id = "user-1"
    cycle.org_id = "org-1"
    cycle.name = "Cycle smoke test"
    cycle.prompt = "Run the smoke test"
    cycle.schedule_expr = schedule_expr
    cycle.timezone = timezone_name
    cycle.enabled = True
    cycle.model_override = None
    cycle.thinking_override = None
    cycle.execution_mode = "new_idea_per_run"
    cycle.target_idea_id = None
    cycle.reopen_archived = False
    cycle.next_run_at = None
    cycle.last_run_at = None
    cycle.last_status = None
    cycle.last_error = None
    cycle.created_at = now
    cycle.updated_at = now
    return cycle


def test_serialize_cycle_does_not_raise_for_legacy_bad_timezone():
    cycle = _cycle_for_serialization(
        schedule_expr="25 8 * * *",
        timezone_name="Not/A_Timezone",
    )

    serialized = service.serialize_cycle(cycle)

    assert serialized["schedule_human"] == "25 8 * * * (Not/A_Timezone)"
    assert serialized["execution_mode"] == service.REUSABLE_THREAD_EXECUTION_MODE
    assert serialized["reopen_archived"] is True


def test_serialize_cycle_does_not_raise_for_legacy_bad_cron():
    cycle = _cycle_for_serialization(
        schedule_expr="not a cron",
        timezone_name="America/Toronto",
    )

    serialized = service.serialize_cycle(cycle)

    assert serialized["schedule_human"] == "not a cron (America/Toronto)"


def test_serialize_cycle_normalizes_uuid_columns_for_api_schema():
    cycle = _cycle_for_serialization(
        schedule_expr="0 9 * * *",
        timezone_name="America/Toronto",
    )
    cycle.user_id = uuid4()
    cycle.org_id = uuid4()
    cycle.target_idea_id = uuid4()

    serialized = service.serialize_cycle(cycle)

    assert isinstance(serialized["user_id"], str)
    assert isinstance(serialized["org_id"], str)
    assert isinstance(serialized["target_idea_id"], str)
    CycleRead.model_validate(serialized)


def test_serialize_cycle_falls_back_for_legacy_missing_timestamps():
    cycle = _cycle_for_serialization(
        schedule_expr="0 9 * * *",
        timezone_name="America/Toronto",
    )
    cycle.created_at = None
    cycle.updated_at = None

    serialized = service.serialize_cycle(cycle)

    assert serialized["created_at"] is not None
    assert serialized["updated_at"] is not None
    CycleRead.model_validate(serialized)


def test_serialize_cycle_run_normalizes_uuid_columns_for_api_schema():
    run = CycleRun()
    run.id = 7
    run.cycle_id = 42
    run.scheduled_for = datetime(2026, 4, 28, 12, 0, tzinfo=timezone.utc)
    run.started_at = None
    run.completed_at = None
    run.status = "completed"
    run.error = None
    run.skip_reason = None
    run.idea_id = uuid4()
    run.run_id = 99
    run.prompt_snapshot = "Run the smoke test"
    run.created_at = None

    serialized = service.serialize_cycle_run(run)

    assert isinstance(serialized["idea_id"], str)
    assert serialized["created_at"] == run.scheduled_for
    CycleRunRead.model_validate(serialized)


def test_cycle_run_creation_uses_typed_admission(monkeypatch):
    from brain.systems.runs.cortex import RunAdmissionResult

    calls = []

    def fake_admit(request, *, session=None):
        calls.append((request, session))
        return RunAdmissionResult(ok=True, run_id=77)

    monkeypatch.setattr(service, "admit_run", fake_admit)
    session = object()

    run_id = service._admit_cycle_run(
        session,
        idea_id="idea-1",
        message="cycle prompt",
        priority=1,
        user_id="user-1",
        metadata={"source": "cycle", "cycle_run_id": 12},
        cycle_run_id=12,
    )

    assert run_id == 77
    request, passed_session = calls[0]
    assert passed_session is session
    assert request.source == "cycle"
    assert request.producer == "cycle"
    assert request.idempotency_key == "cycle_run:12"
    assert request.metadata["source"] == "cycle"
    assert request.metadata["cycle_run_id"] == 12


def test_execute_cycle_run_logs_uuid_idea_id_without_slicing_error(monkeypatch):
    idea_id = uuid4()

    run = CycleRun()
    run.id = 12
    run.cycle_id = 5
    run.status = "queued"
    run.scheduled_for = datetime(2026, 4, 28, 20, 20, tzinfo=timezone.utc)
    run.prompt_snapshot = "Summarize the newest news"

    cycle = Cycle()
    cycle.id = 5
    cycle.user_id = "user-1"
    cycle.org_id = "org-1"
    cycle.name = "Daily Anthropic news summary"
    cycle.prompt = "Summarize the newest news"
    cycle.schedule_expr = "20 16 * * *"
    cycle.timezone = "America/Toronto"
    cycle.target_idea_id = idea_id
    cycle.deleted_at = None
    cycle.model_override = None
    cycle.thinking_override = None

    idea = Idea()
    idea.id = idea_id
    idea.title = "Daily Anthropic news summary"
    idea.display_title = None
    idea.description = "Summarize the newest news"
    idea.status = "needs_input"
    idea.origin = "cycle"
    idea.origin_ref = "cycle:5"
    idea.salience_score = None
    idea.position_x = None
    idea.position_y = None
    idea.created_at = None
    idea.updated_at = None
    idea.user_id = "user-1"
    idea.org_id = "org-1"
    idea.archived_at = None
    idea.active_agents = []
    idea.attachments = []

    session = _ExecuteCycleSession(run=run, cycle=cycle, idea=idea)
    monkeypatch.setattr(service, "UnitOfWork", lambda: _ExecuteCycleUoW(session))
    monkeypatch.setattr(service, "_admit_cycle_run", lambda *args, **kwargs: 77)
    monkeypatch.setattr(service, "publish", lambda *args, **kwargs: None)
    monkeypatch.setattr(service, "_capture_cycle_emotion", lambda *args, **kwargs: None)

    service.execute_cycle_run(run.id)

    assert run.idea_id == idea_id
    assert run.run_id == 77
    assert run.status == "running"
    assert cycle.last_status == "running"


def test_finalize_cycle_run_from_run_updates_cycle_and_run(monkeypatch):
    agent_run = AgentRun()
    agent_run.metadata_ = {"source": "cycle", "cycle_run_id": 12}

    cycle_run = CycleRun()
    cycle_run.id = 12
    cycle_run.cycle_id = 5
    cycle_run.status = "running"
    cycle_run.completed_at = None
    cycle_run.error = None
    cycle_run.skip_reason = None

    cycle = Cycle()
    cycle.id = 5
    cycle.last_status = None
    cycle.last_error = "old"
    cycle.last_run_at = None

    monkeypatch.setattr(
        service,
        "UnitOfWork",
        lambda: _FakeUoW(agent_run=agent_run, run=cycle_run, cycle=cycle),
    )

    service.finalize_cycle_run_from_run(44, status="completed")

    assert cycle_run.status == "completed"
    assert cycle_run.completed_at is not None
    assert cycle_run.error is None
    assert cycle.last_status == "completed"
    assert cycle.last_error is None
    assert cycle.last_run_at is not None


def test_finalize_cycle_run_from_run_ignores_non_cycle_run(monkeypatch):
    agent_run = AgentRun()
    agent_run.metadata_ = {"source": "user"}

    cycle_run = CycleRun()
    cycle_run.status = "running"
    cycle = Cycle()

    monkeypatch.setattr(
        service,
        "UnitOfWork",
        lambda: _FakeUoW(agent_run=agent_run, run=cycle_run, cycle=cycle),
    )

    service.finalize_cycle_run_from_run(44, status="failed", error="boom")

    assert cycle_run.status == "running"
    assert cycle.last_status is None


def test_cycle_route_scope_uses_workspace_when_available():
    conditions = cycles_router._cycle_scope_conditions(
        {"id": "user-2", "org_id": "org-1", "principal_type": "human"}
    )

    compiled = str(
        select(Cycle.id).where(*conditions).compile(compile_kwargs={"literal_binds": True})
    )

    assert "cycles.org_id = 'org-1'" in compiled
    assert "cycles.org_id IS NULL" in compiled
    assert "users.org_id = 'org-1'" in compiled


def test_cycle_target_idea_scope_uses_workspace_when_available():
    conditions = cycles_router._target_idea_scope_conditions(
        "idea-1",
        {"id": "user-2", "org_id": "org-1", "principal_type": "human"},
    )

    compiled = str(
        select(Idea.id).where(*conditions).compile(compile_kwargs={"literal_binds": True})
    )

    assert "ideas.id = 'idea-1'" in compiled
    assert "ideas.org_id = 'org-1'" in compiled
    assert "ideas.org_id IS NULL" in compiled
    assert "users.org_id = 'org-1'" in compiled


def test_cycle_executor_target_idea_scope_uses_workspace_with_legacy_fallback():
    cycle = Cycle()
    cycle.user_id = "user-1"
    cycle.org_id = "org-1"

    compiled = str(
        select(Idea.id)
        .where(service._cycle_target_idea_scope_condition(cycle))
        .compile(compile_kwargs={"literal_binds": True})
    )

    assert "ideas.org_id = 'org-1'" in compiled
    assert "ideas.org_id IS NULL" in compiled
    assert "users.org_id = 'org-1'" in compiled


def test_finalize_cycle_run_from_run_skips_terminal_runs(monkeypatch):
    agent_run = AgentRun()
    agent_run.metadata_ = {"source": "cycle", "cycle_run_id": 12}

    cycle_run = CycleRun()
    cycle_run.id = 12
    cycle_run.cycle_id = 5
    cycle_run.status = "completed"

    cycle = Cycle()
    cycle.id = 5
    cycle.last_status = None

    monkeypatch.setattr(
        service,
        "UnitOfWork",
        lambda: _FakeUoW(agent_run=agent_run, run=cycle_run, cycle=cycle),
    )

    service.finalize_cycle_run_from_run(44, status="failed", error="boom")

    assert cycle_run.status == "completed"
    assert cycle.last_status is None
