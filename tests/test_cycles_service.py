from __future__ import annotations

import json
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


class _AsyncFakeSession(_FakeSession):
    async def get(self, model, value):
        return super().get(model, value)


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


class _AllResult:
    def __init__(self, values):
        self._values = list(values)

    def all(self):
        return list(self._values)



class _RouterCycleResult:
    def __init__(self, cycle):
        self._cycle = cycle

    def first(self):
        return self._cycle


class _RouterCycleSession:
    def __init__(self, cycle):
        self._cycle = cycle
        self.flushed = False

    async def scalars(self, statement):
        return _RouterCycleResult(self._cycle)

    async def execute(self, statement):
        return _FirstResult((self._cycle.target_idea_id,))

    async def flush(self):
        self.flushed = True

    async def commit(self):
        pass


class _ExecuteCycleSession:
    def __init__(self, *, run, cycle, idea, owner=None, expected_run_id=None):
        self._scalar_values = [run, cycle, idea]
        self._owner = owner
        self.added = []
        self.statements = []
        self.expected_run_id = expected_run_id

    def scalars(self, statement):
        self.statements.append(statement)
        if self.expected_run_id is not None and len(self.statements) == 1:
            params = statement.compile().params
            if self.expected_run_id not in params.values():
                return _ScalarResult(None)
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


class _AsyncUnitOfWorkFactory:
    def __init__(self, sessions):
        self._sessions = list(sessions)
        self.uows = []

    def __call__(self):
        if not self._sessions:
            raise AssertionError("unexpected async UnitOfWork")
        uow = _AsyncUoW(self._sessions.pop(0))
        self.uows.append(uow)
        return uow

    def blocking(self):
        raise AssertionError("sync UnitOfWork bridge should not be used")


class _AsyncUoW:
    def __init__(self, session):
        self.session = session
        self.entered = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def __enter__(self):
        raise AssertionError("sync UnitOfWork bridge should not be used")

    def __exit__(self, exc_type, exc, tb):
        return False


class _AsyncExecuteCycleSession(_ExecuteCycleSession):
    async def scalars(self, statement):
        return super().scalars(statement)

    async def execute(self, statement):
        return super().execute(statement)

    async def get(self, model, value):
        return super().get(model, value)

    async def flush(self):
        super().flush()


class _AsyncRunNowCreateSession:
    def __init__(self, cycle, created_runs):
        self._cycle = cycle
        self._created_runs = created_runs

    async def get(self, model, value):
        if model is Cycle:
            return self._cycle
        return None

    def add(self, value):
        self._created_runs.append(value)

    async def flush(self):
        for value in self._created_runs:
            if isinstance(value, CycleRun) and getattr(value, "id", None) is None:
                value.id = 99
                value.created_at = value.scheduled_for


class _AsyncRunNowLoadSession:
    def __init__(self, run_or_runs):
        self._run_or_runs = run_or_runs

    async def get(self, model, value):
        if model is CycleRun:
            if isinstance(self._run_or_runs, list):
                return self._run_or_runs[0]
            return self._run_or_runs
        return None


class _AsyncCycleListSession:
    def __init__(self, cycles):
        self._cycles = cycles

    async def scalars(self, statement):
        return _AllResult(self._cycles)


def _fail_sync_bridge(*args, **kwargs):
    raise AssertionError("sync DB bridge should not be used")


def test_compute_next_run_at_uses_timezone():
    baseline = datetime(2026, 4, 22, 12, 34, tzinfo=timezone.utc)
    next_run = service.compute_next_run_at(
        "0 9 * * *",
        "America/Toronto",
        from_dt=baseline,
    )
    assert next_run == datetime(2026, 4, 22, 13, 0, tzinfo=timezone.utc)


def test_humanize_schedule_names_single_monday_not_weekdays():
    assert service.humanize_schedule("0 9 * * 1", "America/Toronto") == "Mondays at 9:00 AM (America/Toronto)"


def test_one_time_schedule_uses_timezone_and_expires_after_run():
    expr = service.validate_schedule_expr(
        "at:2026-05-08T09:30:00",
        "America/Toronto",
    )

    next_run = service.compute_next_run_at(expr, "America/Toronto")

    assert expr.startswith("at:")
    assert next_run == datetime(2026, 5, 8, 13, 30, tzinfo=timezone.utc)
    assert (
        service.compute_next_run_at(
            expr,
            "America/Toronto",
            from_dt=next_run,
        )
        is None
    )


def test_humanize_one_time_schedule():
    label = service.humanize_schedule(
        "at:2026-05-08T09:30:00",
        "America/Toronto",
    )

    assert label == "Once at May 8, 2026 9:30 AM (America/Toronto)"


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


def test_cycle_router_bad_request_returns_400():
    with pytest.raises(cycles_router.HTTPException) as caught:
        raise cycles_router._bad_request(ValueError("Unknown timezone: Mars/Base"))

    assert caught.value.status_code == 400
    assert caught.value.detail == "Unknown timezone: Mars/Base"


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



@pytest.mark.asyncio
async def test_cycle_update_recomputes_schedule_with_new_timezone_before_saving():
    cycle = _cycle_for_serialization(
        schedule_expr="0 9 * * *",
        timezone_name="UTC",
    )
    db = _RouterCycleSession(cycle)

    body = cycles_router.CycleUpdate(
        schedule_expr="0 9 * * 1",
        timezone="America/Toronto",
    )
    response = await cycles_router.update_cycle(
        cycle.id,
        body,
        db=db,
        user={"id": cycle.user_id, "org_id": None},
    )

    assert response["schedule_expr"] == "0 9 * * 1"
    assert response["timezone"] == "America/Toronto"
    assert response["next_run_at"].tzinfo is not None
    assert cycle.schedule_expr == "0 9 * * 1"
    assert cycle.timezone == "America/Toronto"
    assert db.flushed is True


@pytest.mark.asyncio
async def test_cycle_update_validation_failure_does_not_mutate_or_flush():
    cycle = _cycle_for_serialization(
        schedule_expr="0 9 * * *",
        timezone_name="America/Toronto",
    )
    db = _RouterCycleSession(cycle)

    body = cycles_router.CycleUpdate(
        schedule_expr="0 9 * * 1",
        timezone="Mars/Base",
    )
    with pytest.raises(cycles_router.HTTPException) as caught:
        await cycles_router.update_cycle(
            cycle.id,
            body,
            db=db,
            user={"id": cycle.user_id, "org_id": None},
        )

    assert caught.value.status_code == 400
    assert cycle.timezone == "America/Toronto"
    assert cycle.schedule_expr == "0 9 * * *"
    assert db.flushed is False

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


@pytest.mark.asyncio
async def test_async_run_cycle_now_uses_native_uow_without_sync_bridges(monkeypatch):
    cycle = Cycle()
    cycle.id = 5
    cycle.prompt = "Run the smoke test"
    cycle.deleted_at = None

    created_runs = []
    factory = _AsyncUnitOfWorkFactory([
        _AsyncRunNowCreateSession(cycle, created_runs),
        _AsyncRunNowLoadSession(created_runs),
    ])
    executed_run_ids = []

    async def fake_async_execute_cycle_run(run_id):
        executed_run_ids.append(run_id)
        created_runs[0].status = "completed"

    monkeypatch.setattr(service, "UnitOfWork", factory)
    monkeypatch.setattr(service, "async_execute_cycle_run", fake_async_execute_cycle_run)
    monkeypatch.setattr(service, "open_unit_of_work", _fail_sync_bridge, raising=False)
    monkeypatch.setattr(service, "run_unit_of_work_task", _fail_sync_bridge, raising=False)

    payload = await service.async_run_cycle_now(cycle.id)

    assert executed_run_ids == [99]
    assert payload["id"] == 99
    assert payload["cycle_id"] == cycle.id
    assert payload["prompt_snapshot"] == cycle.prompt
    assert payload["status"] == "completed"
    assert all(uow.entered for uow in factory.uows)


@pytest.mark.asyncio
async def test_cycle_run_creation_uses_typed_admission(monkeypatch):
    from brain.systems.runs.work_intake import WorkIntakeResult

    calls = []

    async def fake_admit(session, event):
        calls.append((session, event))
        return WorkIntakeResult(ok=True, run_id=77)

    monkeypatch.setattr(service, "admit_work", fake_admit)
    session = object()

    run_id = await service._async_admit_cycle_run(
        session,
        idea_id="idea-1",
        message="cycle prompt",
        priority=1,
        user_id="user-1",
        metadata={"source": "cycle", "cycle_run_id": 12},
        cycle_run_id=12,
    )

    assert run_id == 77
    passed_session, event = calls[0]
    assert passed_session is session
    assert event.source == "cycle"
    assert event.event_type == "cycle.due_run"
    assert event.target == {"kind": "cortex_idea", "idea_id": "idea-1"}
    assert event.payload["metadata"]["source"] == "cycle"
    assert event.payload["metadata"]["cycle_run_id"] == 12
    assert event.policy["producer"] == "cycle"
    assert event.policy["idempotency_key"] == "cycle_run:12"
    assert event.policy["run_event"] == "thread_reply"


@pytest.mark.asyncio
async def test_execute_cycle_run_logs_uuid_idea_id_without_slicing_error(monkeypatch):
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

    session = _AsyncExecuteCycleSession(run=run, cycle=cycle, idea=idea, expected_run_id=run.id)
    monkeypatch.setattr(
        service,
        "UnitOfWork",
        _AsyncUnitOfWorkFactory([session]),
    )
    admissions = []

    async def fake_admit(*args, **kwargs):
        admissions.append(kwargs)
        return 77

    monkeypatch.setattr(service, "_async_admit_cycle_run", fake_admit)
    monkeypatch.setattr(service, "publish", lambda *args, **kwargs: None)
    monkeypatch.setattr(service, "_capture_cycle_emotion", lambda *args, **kwargs: None, raising=False)

    await service.async_execute_cycle_run(run.id)

    assert run.idea_id == idea_id
    assert run.run_id == 77
    assert run.status == "running"
    assert cycle.last_status == "running"
    assert admissions[0]["metadata"]["origin"] == "cycle"
    assert admissions[0]["metadata"]["launch_envelope"]["origin"] == "scheduled_cycle"
    assert admissions[0]["metadata"]["launch_envelope"]["active_instruction_source"] == "cycle.prompt"
    assert admissions[0]["metadata"]["context_policy"]["prior_thread_role"] == "context_only"
    assert admissions[0]["metadata"]["tool_policy"]["disabled_tools"] == ["manage_cycle"]
    assert "Scheduled Prompt Launch" in admissions[0]["message"]
    assert "Prior thread messages are background context only" in admissions[0]["message"]


@pytest.mark.asyncio
async def test_async_execute_cycle_run_uses_native_uow_without_sync_bridges(monkeypatch):
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

    session = _AsyncExecuteCycleSession(run=run, cycle=cycle, idea=idea, expected_run_id=run.id)
    factory = _AsyncUnitOfWorkFactory([session])
    admissions = []

    async def fake_async_admit(*args, **kwargs):
        admissions.append(kwargs)
        return 77

    monkeypatch.setattr(service, "UnitOfWork", factory)
    monkeypatch.setattr(service, "_async_admit_cycle_run", fake_async_admit)
    monkeypatch.setattr(service, "publish", lambda *args, **kwargs: None)
    monkeypatch.setattr(service, "open_unit_of_work", _fail_sync_bridge, raising=False)
    monkeypatch.setattr(service, "run_unit_of_work_task", _fail_sync_bridge, raising=False)

    await service.async_execute_cycle_run(run.id)

    assert factory.uows[0].entered is True
    assert run.idea_id == idea_id
    assert run.run_id == 77
    assert run.status == "running"
    assert cycle.last_status == "running"
    assert admissions[0]["metadata"]["origin"] == "cycle"
    assert admissions[0]["metadata"]["tool_policy"]["disabled_tools"] == ["manage_cycle"]


@pytest.mark.asyncio
async def test_manage_cycle_list_uses_native_uow_without_sync_bridges(monkeypatch):
    from brain.systems.runs.tool_catalog.handlers import cycles as cycle_handlers

    cycle = _cycle_for_serialization(
        schedule_expr="0 9 * * *",
        timezone_name="America/Toronto",
    )
    factory = _AsyncUnitOfWorkFactory([_AsyncCycleListSession([cycle])])

    monkeypatch.setattr(cycle_handlers, "UnitOfWork", factory)
    monkeypatch.setattr(cycle_handlers, "open_unit_of_work", _fail_sync_bridge, raising=False)
    monkeypatch.setattr(cycle_handlers, "run_unit_of_work_task", _fail_sync_bridge, raising=False)
    monkeypatch.setattr(cycle_handlers._agent_context, "user_id", "user-1", raising=False)
    monkeypatch.setattr(cycle_handlers._agent_context, "org_id", None, raising=False)

    payload = json.loads(await cycle_handlers._handle_manage_cycle_async(action="list"))

    assert factory.uows[0].entered is True
    assert payload["cycles"][0]["id"] == cycle.id
    assert payload["cycles"][0]["name"] == cycle.name


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
        _AsyncUnitOfWorkFactory([
            _AsyncFakeSession(agent_run=agent_run, run=cycle_run, cycle=cycle)
        ]),
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
        _AsyncUnitOfWorkFactory([
            _AsyncFakeSession(agent_run=agent_run, run=cycle_run, cycle=cycle)
        ]),
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

    assert "cycles.org_id =" in compiled
    assert "cycles.org_id IS NULL" in compiled
    assert "users.org_id =" in compiled


def test_cycle_target_idea_scope_uses_workspace_when_available():
    conditions = cycles_router._target_idea_scope_conditions(
        "idea-1",
        {"id": "user-2", "org_id": "org-1", "principal_type": "human"},
    )

    compiled = str(
        select(Idea.id).where(*conditions).compile(compile_kwargs={"literal_binds": True})
    )

    assert "ideas.id =" in compiled
    assert "ideas.org_id =" in compiled
    assert "ideas.org_id IS NULL" in compiled
    assert "users.org_id =" in compiled


def test_cycle_executor_target_idea_scope_uses_workspace_with_legacy_fallback():
    cycle = Cycle()
    cycle.user_id = "user-1"
    cycle.org_id = "org-1"

    compiled = str(
        select(Idea.id)
        .where(service._cycle_target_idea_scope_condition(cycle))
        .compile(compile_kwargs={"literal_binds": True})
    )

    assert "ideas.org_id =" in compiled
    assert "ideas.org_id IS NULL" in compiled
    assert "users.org_id =" in compiled


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
        _AsyncUnitOfWorkFactory([
            _AsyncFakeSession(agent_run=agent_run, run=cycle_run, cycle=cycle)
        ]),
    )

    service.finalize_cycle_run_from_run(44, status="failed", error="boom")

    assert cycle_run.status == "completed"
    assert cycle.last_status is None
