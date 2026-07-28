"""Postgres-backed concurrency coverage for the Cycle wake and claim locks.

``async_wake_cycle_now`` (PR #536) serializes competing callers with
``SELECT ... FOR UPDATE``, and the scheduler's claim loop takes the same rows
with ``FOR UPDATE SKIP LOCKED``. The fast suite drives both through one shared
SQLite session, where row locks are a no-op — so the lock ordering these paths
depend on is unproven there. These tests give every caller its own committed
transaction against the CI Postgres service, which is the only place those
locks are real.

Two consequences shape the fixtures below. Rows must be committed rather than
held open in a rollback session (the rest of the DB lane's idiom), because two
callers cannot contend for a lock inside one transaction. And the scheduler
selects *every* due cycle in the database, so committed rows from elsewhere
would silently steer these tests — hence a private schema per test rather than
seeding into the shared one.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.schema import CreateTable

from brain.platform.db.models.cycle import (
    Cycle,
    CycleGuidance,
    CycleOutputTarget,
    CycleRevision,
    CycleRun,
    CycleRunEvaluation,
)
from brain.platform.db.models.org import Org, User
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.cycles import service
from brain.systems.cycles.schedules import compute_next_run_at
from tests.conftest import TEST_DB_URL
from tests.db_engine_utils import create_async_test_engine

pytestmark = [pytest.mark.asyncio, pytest.mark.requires_db]

# Every minute: the imminent cron slot the wake-consumption boundary needs.
EVERY_MINUTE = "* * * * *"
# Room between seeding a slot and a caller reaching it. Only ever a guard
# against a loaded runner stalling a test's own setup — no invariant under test
# depends on wall-clock timing.
SLOT_ARRIVAL_MARGIN = timedelta(seconds=5)
LOCK_WAIT_TIMEOUT_SECONDS = 15.0
# A race that has not settled by now is wedged, not slow. Failing here beats
# hanging the DB lane until the job timeout.
RACE_TIMEOUT_SECONDS = 30.0

# Creation order matters: each table's foreign keys resolve through the private
# schema's search_path, so a referenced table must already exist there.
# Anything absent (ideas, agent_runs) resolves to the migrated public schema,
# which is fine — these tests never populate those columns.
_SCHEMA_TABLES = (
    Org.__table__,
    User.__table__,
    Cycle.__table__,
    CycleRevision.__table__,
    CycleGuidance.__table__,
    CycleOutputTarget.__table__,
    CycleRun.__table__,
    CycleRunEvaluation.__table__,
)


@dataclass
class _WakeWorkspace:
    """A private schema, its committed org/user rows, and the cycles under them.

    Every connection handed out here carries the same ``application_name``, so
    lock accounting can tell this test's backends from anything else on the
    server.
    """

    unit_of_work: type[UnitOfWork]
    engine: object
    app_name: str
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

    async def add_queued_run(self, cycle_id: int) -> CycleRun:
        async with self.unit_of_work() as uow:
            run = CycleRun(
                cycle_id=cycle_id,
                scheduled_for=datetime.now(timezone.utc),
                prompt_snapshot="Evaluate promotion readiness.",
                status="queued",
            )
            uow.session.add(run)
            await uow.session.flush()
            return run

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

    @asynccontextmanager
    async def held_row_lock(self, cycle_id: int):
        """Pin one cycles row from an outside connection, yielding it to the test.

        Committing on the yielded connection releases the lock early, which is
        how a test stages a competing write for the blocked caller to find.
        """
        connection = await self.engine.connect()
        try:
            await connection.execute(
                text("SELECT id FROM cycles WHERE id = :cycle_id FOR UPDATE"),
                {"cycle_id": cycle_id},
            )
            yield connection
        finally:
            await connection.rollback()
            await connection.close()

    async def await_lock_waiters(
        self,
        expected: int,
        *,
        timeout: float = LOCK_WAIT_TIMEOUT_SECONDS,
    ) -> None:
        """Block until ``expected`` of this test's backends are blocked on a lock.

        Polling is what makes the races deterministic: a test proceeds once the
        contention it asserts about actually exists, rather than after a guessed
        sleep. Scoping by application_name and pg_blocking_pids keeps unrelated
        backends — other suites, autovacuum, the poller itself — out of the
        count. AUTOCOMMIT keeps each poll on a fresh snapshot.
        """
        connection = await (await self.engine.connect()).execution_options(
            isolation_level="AUTOCOMMIT"
        )
        try:
            deadline = asyncio.get_running_loop().time() + timeout
            blocked = 0
            while asyncio.get_running_loop().time() < deadline:
                blocked = int(
                    (
                        await connection.execute(
                            text(
                                "SELECT count(*) FROM pg_stat_activity "
                                "WHERE datname = current_database() "
                                "AND application_name = :app_name "
                                "AND cardinality(pg_blocking_pids(pid)) > 0"
                            ),
                            {"app_name": self.app_name},
                        )
                    ).scalar_one()
                )
                if blocked >= expected:
                    return
                await asyncio.sleep(0.05)
            raise AssertionError(
                f"expected {expected} of this test's backend(s) blocked on a "
                f"lock, saw {blocked}"
            )
        finally:
            await connection.close()


@pytest.fixture
async def wake_workspace(monkeypatch):
    """Run the service module's own UnitOfWork against a private schema.

    The subclass exists for two reasons the production class cannot serve here:
    the app engine pools asyncpg connections across the event loops pytest hands
    each test, and its rows would land in the shared schema the scheduler scans
    globally. NullPool plus a per-test search_path fixes both while leaving
    transaction isolation and row-lock semantics exactly as production has them.
    """
    schema = f"wake_conc_{uuid4().hex[:12]}"
    app_name = f"wake-conc-{uuid4().hex[:8]}"

    admin_engine = create_async_test_engine(TEST_DB_URL)
    admin = await (await admin_engine.connect()).execution_options(
        isolation_level="AUTOCOMMIT"
    )
    await admin.execute(text(f'CREATE SCHEMA "{schema}"'))

    engine = create_async_test_engine(
        TEST_DB_URL,
        connect_args={
            "server_settings": {
                "search_path": f'"{schema}",public',
                "application_name": app_name,
            }
        },
    )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    class _PrivateSchemaUnitOfWork(UnitOfWork):
        async def __aenter__(self):
            self._async_session = factory()
            return self

    monkeypatch.setattr(service, "UnitOfWork", _PrivateSchemaUnitOfWork)

    workspace = _WakeWorkspace(
        unit_of_work=_PrivateSchemaUnitOfWork,
        engine=engine,
        app_name=app_name,
        org_id=str(uuid4()),
        user_id=str(uuid4()),
    )
    try:
        async with engine.begin() as connection:
            for table in _SCHEMA_TABLES:
                await connection.execute(CreateTable(table))

        async with _PrivateSchemaUnitOfWork() as uow:
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
        yield workspace
    finally:
        await engine.dispose()
        await admin.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        await admin.close()
        await admin_engine.dispose()


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

    async with wake_workspace.held_row_lock(cycle.id):
        before = datetime.now(timezone.utc)
        first = asyncio.create_task(service.async_wake_cycle_now(name=name))
        second = asyncio.create_task(service.async_wake_cycle_now(name=name))
        await wake_workspace.await_lock_waiters(2)
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


async def test_wake_that_waited_past_its_slot_reports_already_pending(wake_workspace):
    """A caller judges the slot as of when it holds the row, not when it asked.

    The slot falls due while the wake is queued on the lock. Sampling the clock
    at entry would call that slot future and re-stamp it; sampling it under the
    lock is what makes this ``already_pending``.
    """
    name = f"Wake Overtaken {uuid4().hex[:8]}"
    slot = datetime.now(timezone.utc) + timedelta(seconds=2)
    cycle = await wake_workspace.add_cycle(name=name, next_run_at=slot)

    async with wake_workspace.held_row_lock(cycle.id):
        wake = asyncio.create_task(service.async_wake_cycle_now(name=name))
        await wake_workspace.await_lock_waiters(1)
        # Hold the row until the slot the wake was told to move has come due.
        await asyncio.sleep(
            max(0.0, (slot - datetime.now(timezone.utc)).total_seconds()) + 0.3
        )

    assert await asyncio.wait_for(wake, RACE_TIMEOUT_SECONDS) == "already_pending"
    assert (await wake_workspace.cycle_row(cycle.id)).next_run_at == slot


async def test_wake_refuses_a_cycle_renamed_while_it_waited(wake_workspace):
    """A wake resolves a name, so it must still hold that name under the lock.

    The name is what the caller asked for; the id is only how the wake reaches
    it. If the row stops matching while the caller waits, waking it anyway
    moves a cycle nobody asked to move.
    """
    name = f"Wake Renamed {uuid4().hex[:8]}"
    scheduled = datetime.now(timezone.utc) + timedelta(days=1)
    cycle = await wake_workspace.add_cycle(name=name, next_run_at=scheduled)

    async with wake_workspace.held_row_lock(cycle.id) as lock:
        wake = asyncio.create_task(service.async_wake_cycle_now(name=name))
        await wake_workspace.await_lock_waiters(1)
        await lock.execute(
            text("UPDATE cycles SET name = :renamed WHERE id = :cycle_id"),
            {"renamed": f"{name} (renamed)", "cycle_id": cycle.id},
        )
        await lock.commit()

    assert await asyncio.wait_for(wake, RACE_TIMEOUT_SECONDS) == "not_found"
    assert (await wake_workspace.cycle_row(cycle.id)).next_run_at == scheduled


async def test_concurrent_claims_start_a_cycle_run_once(wake_workspace):
    """Two executors racing for one queued run: exactly one claim succeeds.

    The owning Cycle's lock does not stop the second executor from arriving —
    it guarantees it arrives *after* the first one committed, which is exactly
    when the post-lock status check has to be reading the row it just locked.
    """
    name = f"Claim Race {uuid4().hex[:8]}"
    cycle = await wake_workspace.add_cycle(name=name, next_run_at=None)
    run = await wake_workspace.add_queued_run(cycle.id)

    async def claim() -> str | None:
        async with wake_workspace.unit_of_work() as uow:
            claimed = await service.async_claim_cycle_run(uow.session, run.id)
            return claimed[0].status if claimed else None

    outcomes = await asyncio.wait_for(
        asyncio.gather(claim(), claim()), RACE_TIMEOUT_SECONDS
    )

    assert [outcome for outcome in outcomes if outcome is not None] == ["running"]
    runs = await wake_workspace.cycle_runs(cycle.id)
    assert [(row.id, row.status) for row in runs] == [(run.id, "running")]


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
        # Daily, so the backstop the scheduler recomputes is still hours away
        # when the wake finally gets the row. A minute-granularity cron would
        # let that backstop fall due first on a slow runner, at which point
        # already_pending is the honest answer and this test is asserting the
        # wrong thing rather than catching a regression.
        schedule_expr="0 11 * * *",
    )

    scheduler = asyncio.create_task(service.async_schedule_due_cycles_once(limit=50))
    await asyncio.wait_for(claim_held.wait(), LOCK_WAIT_TIMEOUT_SECONDS)

    wake = asyncio.create_task(service.async_wake_cycle_now(name=name))
    await wake_workspace.await_lock_waiters(1)
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

    # Briefly future, so the wake sees a cycle that is not yet due (locks the
    # row and proceeds past its already_pending guard) while the scheduler that
    # starts afterwards sees one that is.
    due_at = datetime.now(timezone.utc) + SLOT_ARRIVAL_MARGIN
    name = f"Scheduler vs Wake {uuid4().hex[:8]}"
    cycle = await wake_workspace.add_cycle(
        name=name, next_run_at=due_at, schedule_expr=EVERY_MINUTE
    )

    wake = asyncio.create_task(service.async_wake_cycle_now(name=name))
    await asyncio.wait_for(wake_holds_row.wait(), LOCK_WAIT_TIMEOUT_SECONDS)
    assert datetime.now(timezone.utc) < due_at, (
        "wake did not reach the lock before its slot came due; widen "
        "SLOT_ARRIVAL_MARGIN rather than reading this as a race"
    )
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
    # The next minute's slot, offset so the boundary cannot arrive between
    # seeding it and waking it — the wake must find the cycle not yet due.
    imminent = compute_next_run_at(
        EVERY_MINUTE, "UTC", from_dt=datetime.now(timezone.utc) + SLOT_ARRIVAL_MARGIN
    )
    cycle = await wake_workspace.add_cycle(
        name=name, next_run_at=imminent, schedule_expr=EVERY_MINUTE
    )

    assert await service.async_wake_cycle_now(name=name) == "woken"
    woken_slot = (await wake_workspace.cycle_row(cycle.id)).next_run_at
    assert woken_slot < imminent

    materialized = await service.async_schedule_due_cycles_once(limit=50)

    runs = await wake_workspace.cycle_runs(cycle.id)
    assert len(runs) == 1
    assert runs[0].id in materialized, (
        f"run {runs[0].id} was created but not executable: "
        f"status={runs[0].status} context={runs[0].context_snapshot}"
    )
    assert executed == [runs[0].id]
    assert runs[0].scheduled_for == woken_slot

    row = await wake_workspace.cycle_row(cycle.id)
    assert row.next_run_at == compute_next_run_at(
        EVERY_MINUTE, "UTC", from_dt=woken_slot
    )
    assert row.next_run_at > woken_slot


async def test_concurrent_wakes_on_an_ambiguous_name_move_nothing(wake_workspace):
    """Duplicate names refuse under contention too, and leave both slots alone.

    The ambiguity guard runs before any row is locked, so this asserts that
    concurrency does not turn a refusal into a wake of whichever cycle happened
    to be read first — not that ambiguity arising mid-lock is handled.
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
