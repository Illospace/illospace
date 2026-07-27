"""Postgres-backed concurrency coverage for the Cycle wake primitive.

``async_wake_cycle_now`` (PR #536) serializes competing callers with
``SELECT ... FOR UPDATE``, and the scheduler's claim loop takes the same rows
with ``FOR UPDATE SKIP LOCKED``. The fast suite drives both through one shared
SQLite session, where row locks are a no-op — so the lock ordering the wake
primitive depends on is unproven there. These tests give every caller its own
committed transaction against the CI Postgres service, which is the only place
those locks are real.

Rows are committed rather than held open in a rollback session (the rest of the
DB lane's idiom) because two callers cannot contend for a lock inside a single
transaction. ``wake_workspace`` deletes what it seeded; the ``cycles`` children
all cascade.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain.kernel import config
from brain.platform.db.models.cycle import Cycle, CycleRun
from brain.platform.db.models.org import Org, User
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.cycles import service
from brain.systems.cycles.schedules import compute_next_run_at
from tests.db_engine_utils import create_async_test_engine

pytestmark = [pytest.mark.asyncio, pytest.mark.requires_db]

# Every minute: the imminent cron slot the wake-consumption boundary needs.
EVERY_MINUTE = "* * * * *"
LOCK_WAIT_TIMEOUT_SECONDS = 15.0
# A race that has not settled by now is wedged, not slow. Failing here beats
# hanging the DB lane until the job timeout.
RACE_TIMEOUT_SECONDS = 30.0


@dataclass
class _WakeWorkspace:
    """Committed org/user rows and the cycles seeded under them."""

    unit_of_work: type[UnitOfWork]
    org_id: str
    user_id: str
    cycle_ids: list[int] = field(default_factory=list)

    async def add_cycle(
        self,
        *,
        name: str,
        next_run_at: datetime | None,
        schedule_expr: str = "0 11 * * 1-5",
    ) -> Cycle:
        async with self.unit_of_work() as uow:
            cycle = Cycle(
                user_id=self.user_id,
                org_id=self.org_id,
                name=name,
                prompt="Evaluate promotion readiness.",
                schedule_expr=schedule_expr,
                timezone="UTC",
                enabled=True,
                next_run_at=next_run_at,
            )
            uow.session.add(cycle)
            await uow.session.flush()
            self.cycle_ids.append(cycle.id)
            return cycle

    async def set_next_run_at(self, cycle_id: int, value: datetime | None) -> None:
        async with self.unit_of_work() as uow:
            row = await uow.session.get(Cycle, cycle_id)
            row.next_run_at = value

    async def cycle_row(self, cycle_id: int) -> Cycle:
        """Re-read a cycle on a fresh session, never a cached instance."""
        async with self.unit_of_work() as uow:
            return await uow.session.get(Cycle, cycle_id)

    async def cycle_runs(self, cycle_id: int) -> list[CycleRun]:
        async with self.unit_of_work() as uow:
            return list(
                (
                    await uow.session.scalars(
                        select(CycleRun)
                        .where(CycleRun.cycle_id == cycle_id)
                        .order_by(CycleRun.id.asc())
                    )
                ).all()
            )


@pytest.fixture
async def wake_unit_of_work(monkeypatch):
    """Give every ``async with UnitOfWork()`` its own connection on the test DB.

    The service module's own UnitOfWork is the point of these tests — separate
    sessions, real commits — but the app engine it binds to pools asyncpg
    connections, and pytest hands each test a fresh event loop those pooled
    connections do not survive. NullPool keeps every caller on a genuinely
    separate transaction without leaking a connection into the next test.
    """
    engine = create_async_test_engine(config.DB_URL)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    class _NullPoolUnitOfWork(UnitOfWork):
        async def __aenter__(self):
            self._async_session = factory()
            return self

    monkeypatch.setattr(service, "UnitOfWork", _NullPoolUnitOfWork)
    try:
        yield _NullPoolUnitOfWork
    finally:
        await engine.dispose()


@pytest.fixture
async def wake_workspace(wake_unit_of_work):
    workspace = _WakeWorkspace(
        unit_of_work=wake_unit_of_work, org_id=str(uuid4()), user_id=str(uuid4())
    )
    async with wake_unit_of_work() as uow:
        uow.session.add(
            Org(
                id=workspace.org_id,
                name="Wake Concurrency Org",
                slug=f"wake-concurrency-{workspace.org_id[:8]}",
            )
        )
        await uow.session.flush()
        uow.session.add(
            User(
                id=workspace.user_id,
                org_id=workspace.org_id,
                name="Wake Concurrency Owner",
                email=f"wake-{workspace.user_id[:8]}@example.com",
                approved=True,
            )
        )
    try:
        yield workspace
    finally:
        async with wake_unit_of_work() as uow:
            await uow.session.execute(
                delete(Cycle).where(Cycle.id.in_(workspace.cycle_ids))
            )
            await uow.session.execute(delete(User).where(User.id == workspace.user_id))
            await uow.session.execute(delete(Org).where(Org.id == workspace.org_id))


@asynccontextmanager
async def _held_row_lock(cycle_id: int):
    """Pin one cycles row from an outside connection for the block's duration."""
    engine = create_async_test_engine(config.DB_URL)
    connection = await engine.connect()
    try:
        await connection.execute(
            text("SELECT id FROM cycles WHERE id = :cycle_id FOR UPDATE"),
            {"cycle_id": cycle_id},
        )
        yield
    finally:
        await connection.rollback()
        await connection.close()
        await engine.dispose()


async def _await_lock_waiters(
    expected: int,
    *,
    timeout: float = LOCK_WAIT_TIMEOUT_SECONDS,
) -> None:
    """Block until ``expected`` backends are queued behind a lock.

    Polling pg_stat_activity is what makes these races deterministic: the test
    proceeds when the contention it is asserting about actually exists, instead
    of after a guessed sleep. AUTOCOMMIT keeps each poll on a fresh snapshot.
    """
    engine = create_async_test_engine(config.DB_URL)
    try:
        connection = await (await engine.connect()).execution_options(
            isolation_level="AUTOCOMMIT"
        )
        try:
            deadline = asyncio.get_running_loop().time() + timeout
            waiters = 0
            while asyncio.get_running_loop().time() < deadline:
                waiters = int(
                    (
                        await connection.execute(
                            text(
                                "SELECT count(*) FROM pg_stat_activity "
                                "WHERE datname = current_database() "
                                "AND wait_event_type = 'Lock'"
                            )
                        )
                    ).scalar_one()
                )
                if waiters >= expected:
                    return
                await asyncio.sleep(0.05)
            raise AssertionError(
                f"expected {expected} backend(s) waiting on a row lock, saw {waiters}"
            )
        finally:
            await connection.close()
    finally:
        await engine.dispose()


async def test_concurrent_wakes_leave_one_pending_slot(wake_workspace):
    """Two wakes contending for the same cycle produce exactly one wake.

    Both callers are held behind an outside lock so they are provably racing on
    the row, then released together. Whichever serializes second is looking at
    a cycle that is already due, so it must report ``already_pending`` rather
    than re-stamping the slot.
    """
    name = f"Wake Race {uuid4().hex[:8]}"
    cycle = await wake_workspace.add_cycle(
        name=name,
        next_run_at=datetime.now(timezone.utc) + timedelta(days=1),
    )

    async with _held_row_lock(cycle.id):
        before = datetime.now(timezone.utc)
        first = asyncio.create_task(service.async_wake_cycle_now(name=name))
        second = asyncio.create_task(service.async_wake_cycle_now(name=name))
        await _await_lock_waiters(2)
        assert not first.done() and not second.done()

    dispositions = await asyncio.wait_for(
        asyncio.gather(first, second), RACE_TIMEOUT_SECONDS
    )
    after = datetime.now(timezone.utc)

    assert sorted(dispositions) == ["already_pending", "woken"]

    row = await wake_workspace.cycle_row(cycle.id)
    assert row.next_run_at is not None
    assert before <= row.next_run_at <= after
    assert await wake_workspace.cycle_runs(cycle.id) == []


async def test_repeated_wake_races_never_deadlock(wake_workspace):
    """Unsynchronized wake pairs settle the same way whichever backend wins.

    The deterministic race above pins one grant order; this one lets Postgres
    pick, repeatedly, and asserts the invariant survives either outcome without
    wedging or raising a deadlock error.
    """
    name = f"Wake Rounds {uuid4().hex[:8]}"
    cycle = await wake_workspace.add_cycle(
        name=name,
        next_run_at=datetime.now(timezone.utc) + timedelta(days=1),
    )

    for _round in range(5):
        await wake_workspace.set_next_run_at(
            cycle.id, datetime.now(timezone.utc) + timedelta(days=1)
        )
        before = datetime.now(timezone.utc)

        dispositions = await asyncio.wait_for(
            asyncio.gather(
                service.async_wake_cycle_now(name=name),
                service.async_wake_cycle_now(name=name),
            ),
            RACE_TIMEOUT_SECONDS,
        )
        after = datetime.now(timezone.utc)

        assert sorted(dispositions) == ["already_pending", "woken"]
        row = await wake_workspace.cycle_row(cycle.id)
        assert before <= row.next_run_at <= after

    assert await wake_workspace.cycle_runs(cycle.id) == []


async def test_wake_against_held_scheduler_claim_yields_one_run(
    monkeypatch,
    wake_workspace,
):
    """A wake arriving while the scheduler holds the row cannot double-fire.

    The scheduler is paused mid-transaction — after it has locked the cycle and
    inserted the run — so the wake is forced to queue behind the real claim
    lock rather than racing it by luck.
    """
    executed: list[int] = []

    async def _record_only(run_id: int) -> None:
        executed.append(run_id)

    monkeypatch.setattr(service, "async_execute_cycle_run", _record_only)

    claim_held = asyncio.Event()
    release_claim = asyncio.Event()
    original_snapshot = service._async_prepare_cycle_run_memory_snapshot

    async def _pause_holding_the_claim(session, cycle, run):
        await original_snapshot(session, cycle, run)
        claim_held.set()
        await release_claim.wait()

    monkeypatch.setattr(
        service, "_async_prepare_cycle_run_memory_snapshot", _pause_holding_the_claim
    )

    name = f"Wake vs Scheduler {uuid4().hex[:8]}"
    cycle = await wake_workspace.add_cycle(
        name=name,
        next_run_at=datetime.now(timezone.utc) - timedelta(seconds=1),
        schedule_expr=EVERY_MINUTE,
    )

    scheduler = asyncio.create_task(service.async_schedule_due_cycles_once(limit=50))
    await asyncio.wait_for(claim_held.wait(), LOCK_WAIT_TIMEOUT_SECONDS)

    wake = asyncio.create_task(service.async_wake_cycle_now(name=name))
    await _await_lock_waiters(1)
    assert not wake.done()

    release_claim.set()
    disposition, scheduled_run_ids = await asyncio.wait_for(
        asyncio.gather(wake, scheduler), RACE_TIMEOUT_SECONDS
    )

    runs = await wake_workspace.cycle_runs(cycle.id)
    assert len(runs) == 1
    assert runs[0].id in scheduled_run_ids
    assert executed == [runs[0].id]
    # The run the scheduler just claimed is still active, so the wake defers to
    # it instead of stamping a second slot onto the same movement.
    assert disposition == "run_in_flight"

    row = await wake_workspace.cycle_row(cycle.id)
    assert row.next_run_at > runs[0].scheduled_for


async def test_scheduler_skips_a_cycle_an_in_flight_wake_holds(
    monkeypatch,
    wake_workspace,
):
    """The other lock order: a wake holding the row defers the scheduler tick.

    ``FOR UPDATE SKIP LOCKED`` means the scheduler passes over the contended
    cycle rather than blocking on it, and the wake's slot is materialized once
    on the following tick — never once per tick.
    """
    executed: list[int] = []

    async def _record_only(run_id: int) -> None:
        executed.append(run_id)

    monkeypatch.setattr(service, "async_execute_cycle_run", _record_only)

    wake_holds_row = asyncio.Event()
    release_wake = asyncio.Event()
    original_count = service._async_active_cycle_run_count

    async def _pause_holding_the_wake_lock(session, cycle_id, **kwargs):
        result = await original_count(session, cycle_id, **kwargs)
        wake_holds_row.set()
        await release_wake.wait()
        return result

    monkeypatch.setattr(
        service, "_async_active_cycle_run_count", _pause_holding_the_wake_lock
    )

    # Future by a hair: the wake sees a cycle that is not yet due (so it locks
    # the row and proceeds), and the scheduler that starts afterwards sees one
    # that is.
    due_at = datetime.now(timezone.utc) + timedelta(seconds=1)
    name = f"Scheduler vs Wake {uuid4().hex[:8]}"
    cycle = await wake_workspace.add_cycle(
        name=name, next_run_at=due_at, schedule_expr=EVERY_MINUTE
    )

    wake = asyncio.create_task(service.async_wake_cycle_now(name=name))
    await asyncio.wait_for(wake_holds_row.wait(), LOCK_WAIT_TIMEOUT_SECONDS)
    await asyncio.sleep(
        max(0.0, (due_at - datetime.now(timezone.utc)).total_seconds()) + 0.2
    )

    skipped_tick = await asyncio.wait_for(
        service.async_schedule_due_cycles_once(limit=50), RACE_TIMEOUT_SECONDS
    )
    assert await wake_workspace.cycle_runs(cycle.id) == []

    release_wake.set()
    assert await asyncio.wait_for(wake, RACE_TIMEOUT_SECONDS) == "woken"

    materialized_tick = await asyncio.wait_for(
        service.async_schedule_due_cycles_once(limit=50), RACE_TIMEOUT_SECONDS
    )
    runs = await wake_workspace.cycle_runs(cycle.id)
    assert len(runs) == 1
    assert runs[0].id not in skipped_tick
    assert runs[0].id in materialized_tick
    assert executed == [runs[0].id]


async def test_woken_slot_recomputes_onto_a_future_boundary(
    monkeypatch,
    wake_workspace,
):
    """Consuming a woken slot leaves the next cron boundary as the backstop.

    This is the designed-in behavior PR #536 documents: the wake borrows the
    slot, and the recompute lands strictly ahead of it, so the following
    scheduled run is a backstop rather than a duplicate of what just fired.
    """
    executed: list[int] = []

    async def _record_only(run_id: int) -> None:
        executed.append(run_id)

    monkeypatch.setattr(service, "async_execute_cycle_run", _record_only)

    name = f"Wake Boundary {uuid4().hex[:8]}"
    imminent = compute_next_run_at(EVERY_MINUTE, "UTC")
    cycle = await wake_workspace.add_cycle(
        name=name, next_run_at=imminent, schedule_expr=EVERY_MINUTE
    )

    assert await service.async_wake_cycle_now(name=name) == "woken"
    woken_slot = (await wake_workspace.cycle_row(cycle.id)).next_run_at
    assert woken_slot < imminent

    materialized = await service.async_schedule_due_cycles_once(limit=50)

    runs = await wake_workspace.cycle_runs(cycle.id)
    assert len(runs) == 1
    assert runs[0].id in materialized
    assert executed == [runs[0].id]
    assert runs[0].scheduled_for == woken_slot

    row = await wake_workspace.cycle_row(cycle.id)
    assert row.next_run_at == compute_next_run_at(
        EVERY_MINUTE, "UTC", from_dt=woken_slot
    )
    assert row.next_run_at > woken_slot


async def test_concurrent_wakes_on_an_ambiguous_name_move_nothing(wake_workspace):
    """Duplicate names refuse under contention too, and leave both slots alone.

    The ambiguity guard runs before any row is locked, so concurrency must not
    turn a refusal into a wake of whichever cycle happened to be read first.
    """
    name = f"Twin Cycle {uuid4().hex[:8]}"
    scheduled = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(hours=1)
    first = await wake_workspace.add_cycle(name=name, next_run_at=scheduled)
    second = await wake_workspace.add_cycle(name=name, next_run_at=scheduled)

    dispositions = await asyncio.wait_for(
        asyncio.gather(
            service.async_wake_cycle_now(name=name),
            service.async_wake_cycle_now(name=name),
        ),
        RACE_TIMEOUT_SECONDS,
    )

    assert dispositions == ["ambiguous", "ambiguous"]
    for cycle_id in (first.id, second.id):
        row = await wake_workspace.cycle_row(cycle_id)
        assert row.next_run_at == scheduled
        assert await wake_workspace.cycle_runs(cycle_id) == []
