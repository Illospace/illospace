from __future__ import annotations

import pytest

from brain.platform.db.repositories import unit_of_work as uow_module
from brain.platform.db.repositories.unit_of_work import AsyncRepositoryProxy, UnitOfWork, run_sync_with_unit_of_work
from brain.systems.runs import event_log as event_log_module
from brain.systems.runs.cortex import runner as cortex_runner
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
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    async def run_sync(self, fn):
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


@pytest.mark.asyncio
async def test_async_repository_proxy_runs_sync_repo_in_async_session():
    session = _AsyncSession()
    repo = AsyncRepositoryProxy(session, _Repo)

    assert await repo.record("alpha") == ["alpha"]
    assert session.sync_session.values == ["alpha"]


@pytest.mark.asyncio
async def test_unit_of_work_async_lifecycle_commits_and_closes(monkeypatch):
    session = _AsyncSession()
    monkeypatch.setattr(uow_module, "SessionFactory", lambda: session)

    async with UnitOfWork() as uow:
        assert uow.session is session
        assert await uow._repo(_Repo).record("beta") == ["beta"]

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
        assert await uow.ideas.record("idea") == ["idea"]
        assert await uow.idea_threads.record("thread") == ["idea", "thread"]

    assert session.sync_session.values == ["idea", "thread"]


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
async def test_run_sync_with_unit_of_work_reuses_async_sync_session(monkeypatch):
    session = _AsyncSession()
    monkeypatch.setattr(uow_module, "SessionFactory", lambda: session)

    def legacy_shaped_service(value: str) -> list[str]:
        with UnitOfWork() as uow:
            assert uow.session is session.sync_session
            uow.session.values.append(value)
            return list(uow.session.values)

    result = await run_sync_with_unit_of_work(legacy_shaped_service, "gamma")

    assert result == ["gamma"]
    assert session.sync_session.flushes == 1
    assert session.commits == 1
    assert session.closed is True


@pytest.mark.asyncio
async def test_sync_unit_of_work_refuses_legacy_engine_inside_async_runtime():
    with pytest.raises(RuntimeError, match="Synchronous UnitOfWork cannot open the legacy DB engine"):
        with UnitOfWork():
            pass


def test_cortex_runner_db_bridge_uses_sync_path_outside_async_runtime(monkeypatch):
    async def fail_async_bridge(*args, **kwargs):
        raise AssertionError("runner should not spawn a short-lived async DB loop")

    monkeypatch.setattr(
        cortex_runner,
        "run_sync_with_unit_of_work",
        fail_async_bridge,
        raising=False,
    )

    assert cortex_runner._run_db(lambda value: f"sync:{value}", "ok") == "sync:ok"


@pytest.mark.asyncio
async def test_cortex_runner_db_bridge_rejects_calls_inside_async_runtime():
    with pytest.raises(RuntimeError, match="cannot be called from a running event loop"):
        cortex_runner._run_db(lambda: None)


@pytest.mark.asyncio
async def test_async_agent_run_store_delegates_through_run_sync(monkeypatch):
    calls: list[tuple[str, object]] = []

    class _Store:
        def __init__(self, sync_session, *, auto_commit: bool = False) -> None:
            calls.append(("init", sync_session))
            self.auto_commit = auto_commit

        def create_run(self, request):
            calls.append(("create_run", request))
            return {"request": request, "auto_commit": self.auto_commit}

    session = _AsyncSession()
    monkeypatch.setattr(run_store_module, "AgentRunStore", _Store)

    result = await AsyncAgentRunStore(session, auto_commit=True).create_run("request")

    assert result == {"request": "request", "auto_commit": True}
    assert calls == [("init", session.sync_session), ("create_run", "request")]


@pytest.mark.asyncio
async def test_scheduler_async_claim_wrapper_uses_run_sync(monkeypatch):
    session = _AsyncSession()

    def fake_claim(sync_session, run_id, **kwargs):
        assert sync_session is session.sync_session
        return ("run", run_id, kwargs["owner_id"])

    monkeypatch.setattr(scheduler_runtime, "claim_run", fake_claim)

    result = await scheduler_runtime.async_claim_run(session, 42, owner_id="worker-1")

    assert result == ("run", 42, "worker-1")


@pytest.mark.asyncio
async def test_scheduler_async_executor_wrapper_uses_run_sync(monkeypatch):
    session = _AsyncSession()

    def fake_execute(sync_session, run_id, **kwargs):
        assert sync_session is session.sync_session
        return {"run_id": run_id, "owner_id": kwargs["owner_id"]}

    monkeypatch.setattr(scheduler_executor, "execute_scheduler_run", fake_execute)

    result = await scheduler_executor.async_execute_scheduler_run(session, 99, owner_id="worker-2")

    assert result == {"run_id": 99, "owner_id": "worker-2"}


@pytest.mark.asyncio
async def test_async_record_run_event_uses_short_lived_async_store(monkeypatch):
    session = _AsyncSession()
    appended = []

    class _Store:
        def __init__(self, active_session):
            assert active_session is session

        async def _run(self, fn):
            class _SyncStore:
                def require_run(self, run_id: int):
                    assert run_id == 7
                    return type("Run", (), {"root_run_id": 3})()

            return fn(_SyncStore())

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
