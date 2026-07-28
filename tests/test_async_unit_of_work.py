from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from brain.platform.db.models.reconstructive_memory import MemoryNode
from brain.platform.db.repositories import unit_of_work as uow_module
from brain.platform.db.repositories.unit_of_work import (
    AsyncRepositoryProxy,
    UnitOfWork,
)
from brain.systems.runs import event_log as event_log_module
from brain.systems.runs.cortex import runner as cortex_runner
from brain.systems.runs.direct_loop import session_effects as session_effects_module
from brain.systems.runs.direct_loop import telemetry as telemetry_module
from brain.systems.runs import project_execution_env as project_execution_env_module
from brain.systems.runs import store as run_store_module
from brain.systems.runs.store import AsyncAgentRunStore
from brain.app.scheduler import executor as scheduler_executor
from brain.app.scheduler import runtime as scheduler_runtime


class _SyncSession:
    def __init__(self) -> None:
        self.values: list[str] = []
        self.flushes = 0

    def flush(self) -> None:
        self.flushes += 1


class _AsyncSession:
    def __init__(self) -> None:
        self.sync_session = _SyncSession()
        self.run_sync_calls = 0
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    async def run_sync(self, fn):
        self.run_sync_calls += 1
        return fn(self.sync_session)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def close(self) -> None:
        self.closed = True


class _Repo:
    def __init__(self, session: _SyncSession) -> None:
        self._session = session

    def record(self, value: str) -> list[str]:
        self._session.values.append(value)
        return list(self._session.values)


class _NativeAsyncRepo:
    def __init__(self, session: _AsyncSession) -> None:
        self._session = session

    def record(self, value: str) -> list[str]:
        raise AssertionError("native async repository methods should not use run_sync")

    async def a_record(self, value: str) -> list[str]:
        self._session.sync_session.values.append(f"async:{value}")
        return list(self._session.sync_session.values)


class _BaseNativeAsyncRepo:
    def __init__(self, session) -> None:
        self._session = session

    def record(self, value: str) -> list[str]:
        self._session.values.append(f"base:{value}")
        return list(self._session.values)

    async def a_record(self, value: str) -> list[str]:
        self._session.sync_session.values.append(f"base-async:{value}")
        return list(self._session.sync_session.values)


class _OverridesSyncRepo(_BaseNativeAsyncRepo):
    def record(self, value: str) -> list[str]:
        self._session.values.append(f"override:{value}")
        return list(self._session.values)


@pytest.mark.asyncio
async def test_async_repository_proxy_rejects_sync_only_repo_method():
    session = _AsyncSession()
    repo = AsyncRepositoryProxy(session, _Repo)

    with pytest.raises(AttributeError, match="no native async implementation"):
        repo.record("alpha")
    assert session.run_sync_calls == 0


@pytest.mark.asyncio
async def test_async_repository_proxy_prefers_native_async_repo_method():
    session = _AsyncSession()
    repo = AsyncRepositoryProxy(session, _NativeAsyncRepo)

    assert await repo.record("alpha") == ["async:alpha"]
    assert session.sync_session.values == ["async:alpha"]
    assert session.run_sync_calls == 0


@pytest.mark.asyncio
async def test_async_repository_proxy_rejects_subclass_sync_override():
    session = _AsyncSession()
    repo = AsyncRepositoryProxy(session, _OverridesSyncRepo)

    with pytest.raises(AttributeError, match="no native async implementation"):
        repo.record("alpha")
    assert session.run_sync_calls == 0


@pytest.mark.asyncio
async def test_unit_of_work_async_lifecycle_commits_and_closes(monkeypatch):
    session = _AsyncSession()
    monkeypatch.setattr(uow_module, "SessionFactory", lambda: session)

    async with UnitOfWork() as uow:
        assert uow.session is session
        assert await uow._repo(_NativeAsyncRepo).record("beta") == ["async:beta"]

    assert session.commits == 1
    assert session.rollbacks == 0
    assert session.closed is True


@pytest.mark.asyncio
async def test_unit_of_work_async_cortex_idea_and_thread_repositories(monkeypatch):
    session = _AsyncSession()
    monkeypatch.setattr(uow_module, "SessionFactory", lambda: session)
    monkeypatch.setattr(uow_module, "IdeaRepository", _Repo)
    monkeypatch.setattr(uow_module, "IdeaThreadRepository", _Repo)

    async with UnitOfWork() as uow:
        with pytest.raises(AttributeError, match="no native async implementation"):
            uow.ideas.record("idea")
        with pytest.raises(AttributeError, match="no native async implementation"):
            uow.idea_threads.record("thread")

    assert session.run_sync_calls == 0


@pytest.mark.asyncio
async def test_unit_of_work_notifications_is_repository_property(monkeypatch):
    session = _AsyncSession()
    monkeypatch.setattr(uow_module, "SessionFactory", lambda: session)
    monkeypatch.setattr(uow_module, "NotificationEventRepository", _Repo)

    async with UnitOfWork() as uow:
        with pytest.raises(AttributeError, match="no native async implementation"):
            uow.notifications.record("notification")


@pytest.mark.asyncio
async def test_unit_of_work_notifications_prefers_native_async_repository(monkeypatch):
    session = _AsyncSession()
    monkeypatch.setattr(uow_module, "SessionFactory", lambda: session)
    monkeypatch.setattr(uow_module, "NotificationEventRepository", _NativeAsyncRepo)

    async with UnitOfWork() as uow:
        assert await uow.notifications.record("notification") == ["async:notification"]

    assert session.run_sync_calls == 0


@pytest.mark.asyncio
async def test_unit_of_work_async_lifecycle_rolls_back_on_error(monkeypatch):
    session = _AsyncSession()
    monkeypatch.setattr(uow_module, "SessionFactory", lambda: session)

    with pytest.raises(RuntimeError, match="boom"):
        async with UnitOfWork():
            raise RuntimeError("boom")

    assert session.commits == 0
    assert session.rollbacks == 1
    assert session.closed is True


@pytest.mark.asyncio
async def test_unit_of_work_commit_failure_rolls_back_closes_and_preserves_error(monkeypatch):
    class CommitFailureSession(_AsyncSession):
        async def commit(self) -> None:
            self.commits += 1
            raise RuntimeError("commit exploded")

    session = CommitFailureSession()
    monkeypatch.setattr(uow_module, "SessionFactory", lambda: session)

    with pytest.raises(RuntimeError, match="commit exploded"):
        async with UnitOfWork():
            pass

    assert session.commits == 1
    assert session.rollbacks == 1
    assert session.closed is True


def test_cortex_runner_exposes_async_db_boundaries_without_sync_bridge():
    source = inspect.getsource(cortex_runner)

    assert "_run_db" not in source
    assert "_runner_unit_of_work" not in source


def test_async_agent_run_store_uses_native_async_db_path():
    source = inspect.getsource(run_store_module.AsyncAgentRunStore)

    assert "run_session_task" not in source
    assert ".run_sync(" not in source


def test_async_event_log_uses_native_async_store():
    source = inspect.getsource(event_log_module.async_record_run_event)

    assert "._run(" not in source
    assert "run_session_task" not in source
    assert ".run_sync(" not in source


def test_run_runtime_async_entrypoints_do_not_use_sync_bridges():
    sources = [
        inspect.getsource(event_log_module.async_record_run_event),
        inspect.getsource(event_log_module.async_record_run_degraded_event),
        inspect.getsource(telemetry_module.async_record_api_call),
        inspect.getsource(session_effects_module.async_memory_org_for_user),
        inspect.getsource(session_effects_module.async_apply_agent_session_side_effects),
        inspect.getsource(project_execution_env_module._async_current_run_target_context),
        inspect.getsource(project_execution_env_module.async_current_project_bound_env),
        inspect.getsource(project_execution_env_module.async_prepare_project_execution_env),
    ]

    forbidden = [
        "open_unit_of_work",
        "run_unit_of_work_task",
        "run_session_task",
        "run_async_from_sync",
        "asyncio.run",
        "asyncio.to_thread",
        "threading.Thread",
        "ThreadPoolExecutor",
        ".run_sync(",
    ]
    for source in sources:
        for pattern in forbidden:
            assert pattern not in source


def test_scheduler_async_helpers_are_native_async():
    sources = [
        inspect.getsource(scheduler_runtime.async_claim_run),
        inspect.getsource(scheduler_runtime.async_claim_next_due_run),
        inspect.getsource(scheduler_executor.async_execute_scheduler_run),
        inspect.getsource(scheduler_executor.async_drain_scheduler),
    ]

    for source in sources:
        assert "run_session_task" not in source
        assert ".run_sync(" not in source


@pytest.mark.asyncio
async def test_async_record_run_event_uses_short_lived_async_store(monkeypatch):
    session = _AsyncSession()
    appended = []

    class _Store:
        def __init__(self, active_session):
            assert active_session is session

        async def require_run(self, run_id: int):
            assert run_id == 7
            return type("Run", (), {"root_run_id": 3})()

        async def append_event(self, event):
            appended.append(event)
            return event

    monkeypatch.setattr(event_log_module, "AsyncAgentRunStore", _Store)

    event = await event_log_module.async_record_run_event(
        7,
        "run.test",
        {"ok": True},
        session=session,
    )

    assert event is appended[0]
    assert event.run_id == 7
    assert event.root_run_id == 3
    assert event.event_type == "run.test"
    assert event.payload == {"ok": True}


@pytest.mark.asyncio
async def test_async_record_api_call_uses_supplied_async_session():
    class _TelemetrySession:
        def __init__(self) -> None:
            self.executed = []

        async def execute(self, statement, params):
            self.executed.append((statement, params))

        async def rollback(self):
            raise AssertionError("rollback should not be needed for a successful insert")

    session = _TelemetrySession()

    await telemetry_module.async_record_api_call(
        session_id="session-1",
        run_id=7,
        turn=2,
        model="test-model",
        effort="low",
        tokens_input=11,
        tokens_output=13,
        cache_read=3,
        cache_write=5,
        context_messages=8,
        system_prompt_chars=21,
        status="success",
        stop_reason="stop",
        latency_ms=34,
        session=session,
    )

    assert len(session.executed) == 1
    statement, params = session.executed[0]
    assert "agent_api_calls" in str(statement)
    assert params["sid"] == "session-1"
    assert params["did"] == 7
    assert params["trace_id"] == "run:7"
    assert params["effort"] == "low"
    assert params["ti"] == 11
    assert params["to"] == 13


@pytest.mark.asyncio
async def test_async_session_effects_awaits_async_callbacks():
    calls = []
    tokens = SimpleNamespace(input=10, output=5, cache_read=2, cache_creation=1)
    messages = [{"role": "user", "content": "hello"}]

    async def memory_org(user_id):
        calls.append(("memory_org", user_id))
        return "org-from-memory"

    async def harvest(session_id, harvested_messages, **kwargs):
        calls.append(("harvest", session_id, harvested_messages, kwargs))

    async def save(session_id, saved_messages, system_prompt, *token_args):
        calls.append(("save", session_id, saved_messages, system_prompt, token_args))

    effective_org_id = await session_effects_module.async_apply_agent_session_side_effects(
        session_id="session-1",
        messages=messages,
        output="A routine file-change result long enough to exercise session side effects.",
        system_prompt="system",
        tokens=tokens,
        tool_calls_made=["write_file"],
        user_id="user-1",
        metadata={},
        agent_context=SimpleNamespace(org_id=None),
        idea_id="idea-1",
        run_id=42,
        memory_org_for_user=memory_org,
        harvest_session=harvest,
        save_session=save,
    )

    assert effective_org_id == "org-from-memory"
    assert [call[0] for call in calls] == ["memory_org", "harvest", "save"]
    assert calls[1][3]["org_id"] == "org-from-memory"
    assert calls[2][4] == (10, 5, 2, 1)


@pytest.mark.asyncio
async def test_file_touching_routine_answer_creates_no_memory_nodes(
    async_sqlite_session_factory,
    monkeypatch,
):
    session = await async_sqlite_session_factory([MemoryNode.__table__])

    async def add_memory(*, content, memory_type, **kwargs):
        del kwargs
        session.add(
            MemoryNode(
                node_kind="content",
                content_kind=memory_type,
                canonical_label="unexpected automatic memory",
                text=content,
                normalized_key="unexpected automatic memory",
                scope_key="default",
                visibility="private",
            )
        )
        await session.flush()

    monkeypatch.setattr("brain.app.cli.memory.add_memory", add_memory)
    harvest = AsyncMock()

    await session_effects_module.async_apply_agent_session_side_effects(
        session_id="session-routine-file-change",
        messages=[{"role": "user", "content": "Update the file."}],
        output=(
            "Updated the requested configuration file and verified the routine change "
            "without discovering any durable lesson."
        ),
        system_prompt="system",
        tokens=SimpleNamespace(input=10, output=5, cache_read=0, cache_creation=0),
        tool_calls_made=["write_file"],
        user_id="user-1",
        metadata={"org_id": "org-1"},
        persist_session=False,
        harvest_session=harvest,
    )

    harvest.assert_awaited_once()
    assert await session.scalar(select(func.count()).select_from(MemoryNode)) == 0
