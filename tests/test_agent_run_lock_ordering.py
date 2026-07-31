from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import DBAPIError

from brain.systems.runs.domain import RunRecipe
from brain.systems.runs.events import run_event
from brain.systems.runs.status import RunStatus
from brain.systems.runs.store import (
    AsyncAgentRunStore,
    ExecutionClaim,
    _is_postgres_deadlock,
)


class _Rows:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)

    def one_or_none(self):
        if len(self._rows) > 1:
            raise AssertionError("expected at most one row")
        return self._rows[0] if self._rows else None


class _PostgresBind:
    dialect = postgresql.dialect()


async def test_inbound_slack_obligation_answer_only_locks_mutated_tables():
    from brain.systems.runs.open_asks import (
        record_inbound_slack_obligation_answer,
    )

    session = SimpleNamespace(execute=AsyncMock(return_value=_Rows([])))

    settled = await record_inbound_slack_obligation_answer(
        session,
        org_id="11111111-1111-4111-8111-111111111111",
        channel_id="CALERTS",
        thread_ts="1784741786.046759",
        slack_user_id="UREDA",
        message_ts="1784743141.000100",
        answer_text="Issue #1221 is filed.",
    )

    statement = session.execute.await_args.args[0]
    sql = str(statement.compile(dialect=postgresql.dialect()))
    lock_clause = sql.rsplit("FOR UPDATE", maxsplit=1)[1].strip()

    assert settled == 0
    assert "FOR UPDATE OF open_asks, obligation_notices" in sql
    assert lock_clause == "OF open_asks, obligation_notices"


class _SharedRowLocks:
    def __init__(self):
        self._condition = asyncio.Condition()
        self._holders: dict[int, dict[str, str]] = defaultdict(dict)
        self.events: dict[str, list[tuple[str, int]]] = defaultdict(list)
        self.lock_modes: dict[str, list[str]] = defaultdict(list)
        self.contention_observed = asyncio.Event()

    async def acquire(self, transaction: str, run_id: int, mode: str) -> None:
        async with self._condition:
            while self._conflicts(transaction, run_id, mode):
                self.contention_observed.set()
                await self._condition.wait()
            previous_mode = self._holders[run_id].get(transaction)
            strength = {"key_share": 1, "no_key_update": 2, "update": 3}
            if previous_mode is not None and strength[previous_mode] >= strength[mode]:
                return
            self._holders[run_id][transaction] = mode
            self.events[transaction].append(("row", run_id))
            self.lock_modes[transaction].append(mode)

    def advisory(self, transaction: str, run_id: int) -> None:
        self.events[transaction].append(("advisory", run_id))

    async def release(self, transaction: str) -> None:
        async with self._condition:
            for holders in self._holders.values():
                holders.pop(transaction, None)
            self._condition.notify_all()

    def _conflicts(self, transaction: str, run_id: int, mode: str) -> bool:
        other_modes = [
            held_mode
            for owner, held_mode in self._holders[run_id].items()
            if owner != transaction
        ]
        if mode == "key_share":
            return "update" in other_modes
        if mode == "no_key_update":
            return any(held_mode in {"no_key_update", "update"} for held_mode in other_modes)
        return bool(other_modes)


class _ContendingPostgresSession:
    def __init__(
        self,
        locks: _SharedRowLocks,
        *,
        transaction: str,
        root_run_id: int,
        run_status: str = RunStatus.STARTING.value,
        pause_after_first_lock: bool = False,
    ):
        self._locks = locks
        self._transaction = transaction
        self._root_run_id = root_run_id
        self._run_status = run_status
        self._pause_after_first_lock = pause_after_first_lock
        self._lock_calls = 0
        self.first_lock_acquired = asyncio.Event()
        self.resume = asyncio.Event()

    def get_bind(self):
        return _PostgresBind()

    async def scalars(self, statement):
        compiled = statement.compile(dialect=postgresql.dialect())
        sql = str(compiled)
        if "FOR UPDATE" in sql or "FOR KEY SHARE" in sql or "FOR NO KEY UPDATE" in sql:
            run_ids = next(
                (
                    value
                    for value in compiled.params.values()
                    if isinstance(value, list)
                ),
                None,
            )
            selecting_run = run_ids is None
            if selecting_run:
                run_ids = [
                    next(
                        int(value)
                        for value in compiled.params.values()
                        if isinstance(value, int)
                    )
                ]
            if "FOR KEY SHARE" in sql:
                mode = "key_share"
            elif "FOR NO KEY UPDATE" in sql:
                mode = "no_key_update"
            else:
                mode = "update"
            for run_id in sorted(int(value) for value in run_ids):
                await self._locks.acquire(self._transaction, run_id, mode)
            self._lock_calls += 1
            if self._pause_after_first_lock and self._lock_calls == 1:
                self.first_lock_acquired.set()
                await self.resume.wait()
            if selecting_run:
                return _Rows(
                    [
                        SimpleNamespace(
                            id=run_ids[0],
                            root_run_id=self._root_run_id,
                            status=self._run_status,
                        )
                    ]
                )
            return _Rows(run_ids)

        run_id = next(
            int(value)
            for value in compiled.params.values()
            if isinstance(value, int)
        )
        return _Rows(
            [
                SimpleNamespace(
                    id=run_id,
                    root_run_id=self._root_run_id,
                    status=self._run_status,
                )
            ]
        )

    async def scalar(self, _statement):
        return 0

    async def execute(self, statement, _params=None):
        if "pg_advisory_xact_lock" in str(statement):
            self._locks.advisory(self._transaction, int(_params["run_id"]))

    def add(self, _row):
        return None

    async def flush(self):
        return None

    async def release(self):
        await self._locks.release(self._transaction)


async def _write_child_event(
    session: _ContendingPostgresSession,
    *,
    run_id: int,
    root_run_id: int,
) -> None:
    store = AsyncAgentRunStore(session)
    try:
        await store._locked_run(run_id, root_run_id=root_run_id)
        await store.append_event(
            run_event(
                run_id,
                "run.activity",
                {"label": "Project context ready"},
                root_run_id=root_run_id,
            )
        )
    finally:
        await session.release()


async def test_agent_run_lock_order_is_transaction_wide_and_precedes_advisory_locks():
    locks = _SharedRowLocks()
    nested_session = _ContendingPostgresSession(
        locks,
        transaction="nested",
        root_run_id=3065,
        pause_after_first_lock=True,
    )
    outer_session = _ContendingPostgresSession(
        locks,
        transaction="outer",
        root_run_id=3024,
    )

    nested = asyncio.create_task(
        _write_child_event(nested_session, run_id=3067, root_run_id=3065)
    )
    await asyncio.wait_for(nested_session.first_lock_acquired.wait(), timeout=1)
    outer = asyncio.create_task(
        _write_child_event(outer_session, run_id=3065, root_run_id=3024)
    )
    nested_session.resume.set()
    await asyncio.wait_for(asyncio.gather(nested, outer), timeout=1)

    # The outer transaction only mutates the parent row. FOR NO KEY UPDATE is
    # compatible with the nested child's key-share protection of that parent,
    # so child event persistence no longer creates a parent lock convoy.
    assert not locks.contention_observed.is_set()

    for transaction_events in locks.events.values():
        row_lock_ids = [
            run_id for kind, run_id in transaction_events if kind == "row"
        ]
        assert row_lock_ids == sorted(row_lock_ids)
        first_advisory = next(
            index
            for index, (kind, _run_id) in enumerate(transaction_events)
            if kind == "advisory"
        )
        assert all(kind == "row" for kind, _run_id in transaction_events[:first_advisory])
        assert all(
            kind != "row" for kind, _run_id in transaction_events[first_advisory:]
        )


async def test_chantier_nested_anchor_locks_root_before_child_event(monkeypatch):
    import brain.systems.runs.chantier_continuation as continuation

    root_run_id = 3024
    anchor_run_id = 3065
    locks = _SharedRowLocks()
    session = _ContendingPostgresSession(
        locks,
        transaction="chantier",
        root_run_id=root_run_id,
        run_status=RunStatus.COMPLETED.value,
    )
    session.get = AsyncMock(return_value=SimpleNamespace(id=3067))
    monkeypatch.setattr(
        continuation,
        "_fanout_anchor_id",
        AsyncMock(return_value=anchor_run_id),
    )
    monkeypatch.setattr(
        continuation,
        "_spawned_workers",
        AsyncMock(
            return_value=[SimpleNamespace(status=RunStatus.COMPLETED.value)]
        ),
    )
    monkeypatch.setattr(
        continuation,
        "_record_fanout_evidence_health",
        AsyncMock(),
    )
    monkeypatch.setattr(
        continuation,
        "_resolve_chantier_scope",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        AsyncAgentRunStore,
        "has_event_type",
        AsyncMock(return_value=False),
    )

    async def append_generic_event(_session, *, store, anchor, workers):
        assert workers
        await store.append_event(
            run_event(
                anchor.id,
                "run.worker_continuation_queued",
                root_run_id=anchor.root_run_id,
            )
        )
        return 3088

    monkeypatch.setattr(
        continuation,
        "_queue_generic_continuation",
        append_generic_event,
    )

    continuation_id = await continuation.queue_chantier_continuation_for_terminal_run(
        session,
        terminal_run_id=3067,
    )

    assert continuation_id == 3088
    assert locks.events["chantier"] == [
        ("row", root_run_id),
        ("row", anchor_run_id),
        ("advisory", anchor_run_id),
    ]
    assert locks.lock_modes["chantier"] == [
        "no_key_update",
        "no_key_update",
    ]


async def test_lock_only_acquisition_does_not_query_non_postgres_dialects():
    session = SimpleNamespace(
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="sqlite")),
        scalars=AsyncMock(side_effect=AssertionError("lock-only query issued")),
    )

    locked_ids = await AsyncAgentRunStore(session)._acquire_agent_run_locks(
        [3024, 3065],
        key_share=True,
    )

    assert locked_ids == set()
    session.scalars.assert_not_awaited()


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


def test_deadlock_policy_requires_database_error_and_driver_sqlstate():
    assert _is_postgres_deadlock(_wrapped_deadlock())
    assert not _is_postgres_deadlock(DeadlockDetectedError("driver error only"))

    unrelated = RuntimeError("unrelated database failure")
    unrelated.__context__ = DeadlockDetectedError("earlier handled deadlock")
    assert not _is_postgres_deadlock(DBAPIError("SELECT 1", {}, unrelated))

    name_only_driver_error = type(
        "DeadlockDetectedError",
        (RuntimeError,),
        {},
    )("no sqlstate")
    assert not _is_postgres_deadlock(
        DBAPIError("SELECT 1", {}, name_only_driver_error)
    )


async def test_execution_claim_retries_deadlock_at_transaction_boundary(
    caplog,
    monkeypatch,
):
    original_row = _claim_row()
    refreshed_row = _claim_row()

    async def expire_original_row():
        original_row.id = None

    session = SimpleNamespace(
        rollback=AsyncMock(side_effect=expire_original_row),
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
    )
    store = AsyncAgentRunStore(session)
    claim = ExecutionClaim(run_id=3065, token="owner", attempt=1)
    transaction = AsyncMock(side_effect=[_wrapped_deadlock(), claim])
    refresh = AsyncMock(return_value=refreshed_row)
    sleep = AsyncMock()
    monkeypatch.setattr(store, "_try_acquire_execution_claim_transaction", transaction)
    monkeypatch.setattr(store, "refresh_run", refresh)
    monkeypatch.setattr(asyncio, "sleep", sleep)

    with caplog.at_level(logging.WARNING, logger="brain.systems.runs.store"):
        result = await store._try_acquire_execution_claim(original_row, token="owner")

    assert result == claim
    assert transaction.await_count == 2
    session.rollback.assert_awaited_once()
    refresh.assert_awaited_once_with(3065)
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


async def test_execution_claim_cancellation_rolls_back_without_retry(monkeypatch):
    session = SimpleNamespace(
        rollback=AsyncMock(),
        get_bind=lambda: SimpleNamespace(dialect=SimpleNamespace(name="postgresql")),
    )
    store = AsyncAgentRunStore(session)
    transaction = AsyncMock(side_effect=asyncio.CancelledError)
    sleep = AsyncMock()
    monkeypatch.setattr(store, "_try_acquire_execution_claim_transaction", transaction)
    monkeypatch.setattr(asyncio, "sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        await store._try_acquire_execution_claim(_claim_row(), token="owner")

    session.rollback.assert_awaited_once()
    sleep.assert_not_awaited()


async def test_heartbeat_preflight_skips_locks_for_terminal_or_throttled_runs(
    monkeypatch,
):
    now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
    terminal = SimpleNamespace(
        id=3065,
        root_run_id=3024,
        status=RunStatus.COMPLETED.value,
        metadata_={},
    )
    throttled = SimpleNamespace(
        id=3067,
        root_run_id=3024,
        status=RunStatus.RUNNING.value,
        metadata_={"runner_heartbeat": {"at": (now - timedelta(seconds=5)).isoformat()}},
    )
    session = SimpleNamespace(flush=AsyncMock())
    store = AsyncAgentRunStore(session)
    refresh = AsyncMock(side_effect=[terminal, throttled])
    lock = AsyncMock(side_effect=AssertionError("heartbeat acquired row locks"))
    monkeypatch.setattr(store, "refresh_run", refresh)
    monkeypatch.setattr(store, "_locked_run", lock)

    assert not await store.heartbeat_run(terminal.id, now=now)
    assert not await store.heartbeat_run(
        throttled.id,
        now=now,
        min_interval_seconds=60,
    )

    lock.assert_not_awaited()
    session.flush.assert_not_awaited()


@pytest.mark.parametrize("changed_condition", ["status", "interval"])
async def test_heartbeat_rechecks_write_conditions_under_lock(
    changed_condition,
    monkeypatch,
):
    now = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)
    snapshot = SimpleNamespace(
        id=3065,
        root_run_id=3024,
        status=RunStatus.RUNNING.value,
        metadata_={},
    )
    locked = SimpleNamespace(
        id=3065,
        root_run_id=3024,
        status=(
            RunStatus.COMPLETED.value
            if changed_condition == "status"
            else RunStatus.RUNNING.value
        ),
        metadata_=(
            {}
            if changed_condition == "status"
            else {
                "runner_heartbeat": {
                    "at": (now - timedelta(seconds=5)).isoformat()
                }
            }
        ),
    )
    session = SimpleNamespace(flush=AsyncMock())
    store = AsyncAgentRunStore(session)
    monkeypatch.setattr(store, "refresh_run", AsyncMock(return_value=snapshot))
    lock = AsyncMock(return_value=locked)
    monkeypatch.setattr(store, "_locked_run", lock)

    assert not await store.heartbeat_run(
        snapshot.id,
        now=now,
        min_interval_seconds=60,
    )

    lock.assert_awaited_once_with(snapshot.id, root_run_id=snapshot.root_run_id)
    session.flush.assert_not_awaited()
