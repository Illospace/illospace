from __future__ import annotations

from types import SimpleNamespace

import pytest

from brain.platform.db.repositories import unit_of_work as uow_module
from brain.platform.db.repositories.unit_of_work import (
    AsyncRepositoryProxy,
    UnitOfWork,
)
from brain.systems.runs import event_log as event_log_module
from brain.systems.runs.direct_loop import session_effects as session_effects_module
from brain.systems.runs.direct_loop import telemetry as telemetry_module


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

    async def auto_encode(tool_calls_made, output, session_id, **kwargs):
        calls.append(("auto", tool_calls_made, output, session_id, kwargs))

    async def harvest(session_id, harvested_messages, **kwargs):
        calls.append(("harvest", session_id, harvested_messages, kwargs))

    async def save(session_id, saved_messages, system_prompt, *token_args):
        calls.append(("save", session_id, saved_messages, system_prompt, token_args))

    effective_org_id = await session_effects_module.async_apply_agent_session_side_effects(
        session_id="session-1",
        messages=messages,
        output="A long enough output to be eligible for auto encode if the tools acted.",
        system_prompt="system",
        tokens=tokens,
        tool_calls_made=["write_file"],
        user_id="user-1",
        metadata={},
        agent_context=SimpleNamespace(org_id=None),
        idea_id="idea-1",
        run_id=42,
        memory_org_for_user=memory_org,
        auto_encode_if_needed=auto_encode,
        harvest_session=harvest,
        save_session=save,
    )

    assert effective_org_id == "org-from-memory"
    assert [call[0] for call in calls] == ["memory_org", "auto", "harvest", "save"]
    assert calls[1][4]["org_id"] == "org-from-memory"
    assert calls[2][3]["org_id"] == "org-from-memory"
    assert calls[3][4] == (10, 5, 2, 1)
