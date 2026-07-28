from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import DBAPIError

from brain.systems.runs.domain import RunRecipe
from brain.systems.runs.events import run_event
from brain.systems.runs.status import RunStatus
from brain.systems.runs.store import AsyncAgentRunStore, ExecutionClaim


class _Rows:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class _PostgresBind:
    dialect = postgresql.dialect()


class _LockRecordingSession:
    def __init__(self, root_run_id: int):
        self.root_run_id = root_run_id
        self.lock_ids: list[int] = []
        self.lock_sql: list[str] = []

    def get_bind(self):
        return _PostgresBind()

    async def scalars(self, statement):
        compiled = statement.compile(dialect=postgresql.dialect())
        bound_ids = next(
            value for value in compiled.params.values() if isinstance(value, list)
        )
        run_id = int(bound_ids[0])
        self.lock_ids.append(run_id)
        self.lock_sql.append(str(compiled))
        await asyncio.sleep(0)
        return _Rows(
            [
                SimpleNamespace(
                    id=run_id,
                    root_run_id=self.root_run_id,
                    status=RunStatus.STARTING.value,
                )
            ]
        )


async def test_sibling_write_locks_acquire_root_then_own_row_concurrently():
    root_run_id = 3024
    sibling_ids = (3065, 3067)
    sessions = [_LockRecordingSession(root_run_id) for _ in sibling_ids]
    stores = [AsyncAgentRunStore(session) for session in sessions]

    await asyncio.gather(
        *(
            store._locked_run(sibling_id, root_run_id=root_run_id)
            for store, sibling_id in zip(stores, sibling_ids, strict=True)
        )
    )

    for session, sibling_id in zip(sessions, sibling_ids, strict=True):
        assert session.lock_ids == [root_run_id, sibling_id]
        assert session.lock_sql[0].endswith("FOR KEY SHARE")
        assert session.lock_sql[1].endswith("FOR UPDATE")


class _EventSession:
    def __init__(self):
        self.calls: list[str] = []

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    async def scalar(self, _statement):
        return 0

    def add(self, _row):
        self.calls.append("add")

    async def flush(self):
        self.calls.append("flush")


async def test_event_stream_takes_ordered_row_locks_before_advisory_lock(monkeypatch):
    session = _EventSession()
    store = AsyncAgentRunStore(session)

    async def lock_rows(run_ids, *, key_share):
        assert sorted(run_ids) == [3024, 3065]
        assert key_share is True
        session.calls.append("rows")
        return {}

    async def lock_event_stream(run_id):
        assert run_id == 3065
        session.calls.append("advisory")

    monkeypatch.setattr(store, "_lock_agent_run_rows", lock_rows)
    monkeypatch.setattr(store, "lock_event_stream", lock_event_stream)

    await store.append_event(
        run_event(
            3065,
            "run.activity",
            {"label": "Project context ready"},
            root_run_id=3024,
        )
    )

    assert session.calls == ["rows", "advisory", "add", "flush"]


class DeadlockDetectedError(RuntimeError):
    sqlstate = "40P01"


def _wrapped_deadlock():
    return DBAPIError(
        "SELECT agent_runs.id FROM agent_runs FOR UPDATE",
        {},
        DeadlockDetectedError("deadlock detected"),
    )


def _claim_row():
    return SimpleNamespace(
        id=3065,
        root_run_id=3024,
        status=RunStatus.STARTING.value,
        execution_attempt=0,
        execution_token=None,
        started_at=None,
        recipe=RunRecipe.WORKER.value,
    )


async def test_execution_claim_retries_deadlock_at_transaction_boundary(
    caplog,
    monkeypatch,
):
    session = SimpleNamespace(
        rollback=AsyncMock(),
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
    )
    store = AsyncAgentRunStore(session)
    row = _claim_row()
    claim = ExecutionClaim(run_id=row.id, token="owner", attempt=1)
    transaction = AsyncMock(side_effect=[_wrapped_deadlock(), claim])
    refresh = AsyncMock(return_value=row)
    sleep = AsyncMock()
    monkeypatch.setattr(store, "_try_acquire_execution_claim_transaction", transaction)
    monkeypatch.setattr(store, "refresh_run", refresh)
    monkeypatch.setattr(asyncio, "sleep", sleep)

    with caplog.at_level(logging.WARNING, logger="brain.systems.runs.store"):
        result = await store._try_acquire_execution_claim(row, token="owner")

    assert result == claim
    assert transaction.await_count == 2
    session.rollback.assert_awaited_once()
    refresh.assert_awaited_once_with(row.id)
    sleep.assert_awaited_once()
    assert "agent_run_deadlock_retry" in caplog.text


async def test_execution_claim_deadlock_retry_is_bounded(caplog, monkeypatch):
    session = SimpleNamespace(
        rollback=AsyncMock(),
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
    )
    store = AsyncAgentRunStore(session)
    row = _claim_row()
    transaction = AsyncMock(side_effect=_wrapped_deadlock())
    refresh = AsyncMock(return_value=row)
    monkeypatch.setattr(store, "_try_acquire_execution_claim_transaction", transaction)
    monkeypatch.setattr(store, "refresh_run", refresh)
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    with (
        caplog.at_level(logging.WARNING, logger="brain.systems.runs.store"),
        pytest.raises(DBAPIError),
    ):
        await store._try_acquire_execution_claim(row, token="owner")

    assert transaction.await_count == 3
    assert session.rollback.await_count == 3
    assert refresh.await_count == 2
    assert caplog.text.count("agent_run_deadlock_retry") == 2
