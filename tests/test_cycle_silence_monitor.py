from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib
import json
from unittest.mock import Mock
from uuid import uuid4

import pytest

from brain.platform.db.models.cycle import (
    Cycle,
    CycleFailureGuardLatch,
    CycleRun,
)
from brain.platform.db.models.vault import VaultConfig
from brain.platform.db.repositories.cycle_silence import CycleReceiptSnapshot
from brain.systems.cycles.common import (
    ILLO_LANE_EXECUTOR_BINDING,
    PERSONAL_AGENT_EXECUTOR_BINDING,
)
from brain.systems.cycles.schedules import (
    compute_latest_run_at,
    compute_next_run_at,
)
from brain.systems.cycles.silence_alerts import async_deliver_cycle_silence_alert
from brain.systems.cycles.silence_monitor import (
    CYCLE_MISSED_RECEIPT_TRIGGER_KIND,
    CycleSilenceMonitor,
    CycleSilenceObservation,
)
from brain.systems.cycles.silence_policy import (
    CYCLE_SILENCE_RUNTIME_SETTINGS_KEY,
    CycleSilenceCandidate,
    CycleSilencePolicy,
    async_cycle_silence_policy,
    evaluate_cycle_silence_candidate,
)
from brain.systems.failure_guard.cycle_latches import CycleAlertLatchStore


NOW = datetime(2026, 8, 17, 13, 0, tzinfo=timezone.utc)
GRACE = timedelta(minutes=30)
MIGRATION_MODULE = (
    "brain.platform.db.alembic.versions.0064_cycle_receipt_monitoring"
)


@pytest.fixture
async def silence_session(
    async_sqlite_session_factory,
    sqlite_postgres_ddl_patch,
):
    return await async_sqlite_session_factory(
        [
            Cycle.__table__,
            CycleRun.__table__,
            CycleFailureGuardLatch.__table__,
            VaultConfig.__table__,
        ]
    )


def _cycle(
    *,
    binding: str,
    schedule_expr: str = "0 9 * * *",
    timezone_name: str = "UTC",
    monitoring_started_at: datetime = NOW - timedelta(days=7),
    name: str = "Laptop release radar",
) -> Cycle:
    return Cycle(
        id=41,
        user_id=str(uuid4()),
        org_id=None,
        name=name,
        prompt="Run the schedule.",
        schedule_expr=schedule_expr,
        timezone=timezone_name,
        enabled=True,
        executor_binding=binding,
        skill_ids=[],
        receipt_monitoring_started_at=monitoring_started_at,
        next_run_at=NOW + timedelta(days=1),
        created_at=monitoring_started_at,
        updated_at=monitoring_started_at,
    )


def _silence_candidate(
    cycle: Cycle,
    *,
    last_receipt_at: datetime | None,
    now: datetime,
    grace_margin: timedelta,
) -> CycleSilenceCandidate | None:
    return evaluate_cycle_silence_candidate(
        CycleReceiptSnapshot(
            cycle_id=cycle.id,
            name=cycle.name,
            executor_binding=cycle.executor_binding,
            schedule_expr=cycle.schedule_expr,
            timezone=cycle.timezone,
            receipt_monitoring_started_at=cycle.receipt_monitoring_started_at,
            created_at=cycle.created_at,
            last_receipt_at=last_receipt_at,
        ),
        now=now,
        grace_margin=grace_margin,
    )


class _FakeLatch:
    def __init__(self) -> None:
        self.cycle_ids: set[int] = set()

    async def claim(self, *, cycle_id: int, alerted_at: datetime) -> bool:
        del alerted_at
        if cycle_id in self.cycle_ids:
            return False
        self.cycle_ids.add(cycle_id)
        return True

    async def release(
        self,
        *,
        cycle_id: int,
        alerted_at: datetime | None = None,
    ) -> None:
        del alerted_at
        self.cycle_ids.discard(cycle_id)


@pytest.mark.parametrize(
    "binding",
    (ILLO_LANE_EXECUTOR_BINDING, PERSONAL_AGENT_EXECUTOR_BINDING),
)
async def test_day_old_receipt_alert_names_schedule_binding_and_times(
    silence_session,
    binding,
):
    cycle = _cycle(binding=binding)
    silence_session.add(cycle)
    await silence_session.flush()
    last_receipt_at = NOW - timedelta(days=1, hours=3, minutes=55)
    silence_session.add(
        CycleRun(
            cycle_id=cycle.id,
            scheduled_for=NOW - timedelta(days=1, hours=4),
            completed_at=last_receipt_at,
            status="completed",
            prompt_snapshot="Run the schedule.",
        )
    )
    await silence_session.flush()

    latch = _FakeLatch()
    deliveries = []

    async def capture_delivery(**kwargs):
        deliveries.append(kwargs)

    async def deliver(candidate):
        await async_deliver_cycle_silence_alert(
            candidate,
            deliver_alert=capture_delivery,
        )

    monitor = CycleSilenceMonitor(
        claim_alert=latch.claim,
        release_alert=latch.release,
        deliver_alert=deliver,
    )
    result = await monitor._check(silence_session, now=NOW)
    repeated = await monitor._check(silence_session, now=NOW + timedelta(minutes=1))

    assert result.alerted_cycle_ids == (cycle.id,)
    assert repeated.alerted_cycle_ids == ()
    assert cycle.executor_binding == binding
    assert len(deliveries) == 1
    alert = deliveries[0]
    assert alert["policy"].channel == "#alerts"
    assert alert["subject"].identity == f"Laptop release radar (#{cycle.id})"
    assert f"Binding: {binding}" in alert["presentation"].summary
    assert f"Expected receipt: {NOW.replace(hour=9).isoformat()}" in (
        alert["presentation"].summary
    )
    assert f"Last receipt: {last_receipt_at.isoformat()}" in (
        alert["presentation"].summary
    )


def test_weekday_every_two_hour_schedule_stays_quiet_all_week_when_healthy():
    cycle = _cycle(
        binding=PERSONAL_AGENT_EXECUTOR_BINDING,
        schedule_expr="0 */2 * * 1-5",
        timezone_name="America/Toronto",
        monitoring_started_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
    )
    start = datetime(2026, 8, 10, 4, 0, tzinfo=timezone.utc)
    last_receipt_at: datetime | None = None
    alerts = []

    for step in range(7 * 24 * 12 + 1):
        now = start + timedelta(minutes=5 * step)
        latest_due = compute_latest_run_at(
            cycle.schedule_expr,
            cycle.timezone,
            at_or_before=now - timedelta(minutes=5),
        )
        if latest_due is not None and latest_due >= cycle.receipt_monitoring_started_at:
            last_receipt_at = latest_due + timedelta(minutes=5)
        candidate = _silence_candidate(
            cycle,
            last_receipt_at=last_receipt_at,
            now=now,
            grace_margin=GRACE,
        )
        if candidate is not None:
            alerts.append(candidate)

    assert alerts == []


def test_weekend_pause_does_not_treat_elapsed_wall_time_as_the_cadence():
    cycle = _cycle(
        binding=PERSONAL_AGENT_EXECUTOR_BINDING,
        schedule_expr="0 */2 * * 1-5",
        timezone_name="America/Toronto",
        monitoring_started_at=datetime(2026, 8, 10, tzinfo=timezone.utc),
    )
    friday_receipt = datetime(2026, 8, 15, 2, 5, tzinfo=timezone.utc)

    for hour in range(48):
        weekend_now = datetime(2026, 8, 15, 3, 0, tzinfo=timezone.utc) + timedelta(
            hours=hour
        )
        assert (
            _silence_candidate(
                cycle,
                last_receipt_at=friday_receipt,
                now=weekend_now,
                grace_margin=GRACE,
            )
            is None
        )

    monday_late = datetime(2026, 8, 17, 4, 31, tzinfo=timezone.utc)
    candidate = _silence_candidate(
        cycle,
        last_receipt_at=friday_receipt,
        now=monday_late,
        grace_margin=GRACE,
    )
    assert candidate is not None
    assert candidate.expected_at == datetime(2026, 8, 17, 4, 0, tzinfo=timezone.utc)


def test_long_cadence_does_not_alert_before_the_next_real_slot():
    cycle = _cycle(
        binding=ILLO_LANE_EXECUTOR_BINDING,
        schedule_expr="0 9 1 * *",
        monitoring_started_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )
    august_receipt = datetime(2026, 8, 1, 9, 5, tzinfo=timezone.utc)

    assert (
        _silence_candidate(
            cycle,
            last_receipt_at=august_receipt,
            now=datetime(2026, 8, 31, 23, 59, tzinfo=timezone.utc),
            grace_margin=GRACE,
        )
        is None
    )
    assert (
        _silence_candidate(
            cycle,
            last_receipt_at=august_receipt,
            now=datetime(2026, 9, 1, 9, 31, tzinfo=timezone.utc),
            grace_margin=GRACE,
        )
        is not None
    )


def test_new_schedule_waits_for_its_first_post_creation_slot():
    cycle = _cycle(
        binding=PERSONAL_AGENT_EXECUTOR_BINDING,
        monitoring_started_at=NOW - timedelta(minutes=1),
    )

    assert (
        _silence_candidate(
            cycle,
            last_receipt_at=None,
            now=NOW,
            grace_margin=GRACE,
        )
        is None
    )
    assert (
        _silence_candidate(
            cycle,
            last_receipt_at=None,
            now=NOW + timedelta(days=1, minutes=31),
            grace_margin=GRACE,
        )
        is not None
    )


async def test_recovery_releases_latch_and_later_silence_realerts():
    latch = _FakeLatch()
    deliveries = []
    silent = True
    candidate = CycleSilenceCandidate(
        cycle_id=41,
        name="Weekday AWS check",
        binding=PERSONAL_AGENT_EXECUTOR_BINDING,
        expected_at=NOW - timedelta(hours=1),
        last_receipt_at=NOW - timedelta(hours=3),
        grace_margin=GRACE,
    )

    async def policy(_session):
        return CycleSilencePolicy(grace_margin=GRACE)

    async def candidates(_session, *, now, grace_margin):
        del now, grace_margin
        return CycleSilenceObservation(
            candidates=(candidate,) if silent else (),
            latched_cycle_ids=frozenset(latch.cycle_ids),
        )

    async def deliver(candidate):
        deliveries.append(candidate)

    monitor = CycleSilenceMonitor(
        policy_provider=policy,
        candidate_provider=candidates,
        claim_alert=latch.claim,
        release_alert=latch.release,
        deliver_alert=deliver,
    )

    first = await monitor._check(Mock(), now=NOW)
    repeated = await monitor._check(Mock(), now=NOW + timedelta(minutes=5))
    silent = False
    recovered = await monitor._check(Mock(), now=NOW + timedelta(minutes=10))
    silent = True
    later = await monitor._check(Mock(), now=NOW + timedelta(hours=3))

    assert first.alerted_cycle_ids == (41,)
    assert repeated.alerted_cycle_ids == ()
    assert recovered.overdue_cycle_ids == ()
    assert later.alerted_cycle_ids == (41,)
    assert len(deliveries) == 2


async def test_failed_delivery_releases_latch_for_the_next_tick():
    latch = _FakeLatch()
    attempts = 0
    candidate = CycleSilenceCandidate(
        cycle_id=41,
        name="Weekday AWS check",
        binding=PERSONAL_AGENT_EXECUTOR_BINDING,
        expected_at=NOW - timedelta(hours=1),
        last_receipt_at=NOW - timedelta(hours=3),
        grace_margin=GRACE,
    )

    async def policy(_session):
        return CycleSilencePolicy(grace_margin=GRACE)

    async def candidates(_session, *, now, grace_margin):
        del now, grace_margin
        return CycleSilenceObservation(
            candidates=(candidate,),
            latched_cycle_ids=frozenset(latch.cycle_ids),
        )

    async def deliver(_candidate):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("Slack unavailable")

    monitor = CycleSilenceMonitor(
        policy_provider=policy,
        candidate_provider=candidates,
        claim_alert=latch.claim,
        release_alert=latch.release,
        deliver_alert=deliver,
    )

    first = await monitor._check(Mock(), now=NOW)
    retry = await monitor._check(Mock(), now=NOW + timedelta(minutes=5))

    assert first.alerted_cycle_ids == ()
    assert retry.alerted_cycle_ids == (41,)
    assert attempts == 2


async def test_database_latch_claim_is_atomic_and_rearms(silence_session):
    cycle = _cycle(binding=ILLO_LANE_EXECUTOR_BINDING)
    silence_session.add(cycle)
    await silence_session.flush()

    store = CycleAlertLatchStore(session=silence_session, cycle_id=cycle.id)
    first = await store.try_claim_latch(
        CYCLE_MISSED_RECEIPT_TRIGGER_KIND,
        NOW,
    )
    repeated = await store.try_claim_latch(
        CYCLE_MISSED_RECEIPT_TRIGGER_KIND,
        NOW + timedelta(minutes=5),
    )
    await store.release_latch(
        CYCLE_MISSED_RECEIPT_TRIGGER_KIND,
        alerted_at=NOW + timedelta(minutes=5),
    )
    fenced = await store.try_claim_latch(
        CYCLE_MISSED_RECEIPT_TRIGGER_KIND,
        NOW + timedelta(minutes=10),
    )
    latched_cycle_ids = await CycleAlertLatchStore.load_cycle_ids_for_trigger(
        silence_session,
        CYCLE_MISSED_RECEIPT_TRIGGER_KIND,
    )
    await store.release_latch(
        CYCLE_MISSED_RECEIPT_TRIGGER_KIND,
        alerted_at=NOW,
    )
    rearmed = await store.try_claim_latch(
        CYCLE_MISSED_RECEIPT_TRIGGER_KIND,
        NOW + timedelta(hours=3),
    )

    assert first is True
    assert repeated is False
    assert fenced is False
    assert latched_cycle_ids == frozenset({cycle.id})
    assert rearmed is True


async def test_grace_margin_loads_from_runtime_state_without_a_deploy(
    silence_session,
):
    silence_session.add(
        VaultConfig(
            key=CYCLE_SILENCE_RUNTIME_SETTINGS_KEY,
            value=json.dumps({"grace_minutes": 90}),
        )
    )
    await silence_session.flush()

    policy = await async_cycle_silence_policy(silence_session)

    assert policy.grace_margin == timedelta(minutes=90)


def test_receipt_monitoring_migration_stacks_on_schedule_bindings(monkeypatch):
    migration = importlib.import_module(MIGRATION_MODULE)
    operations = Mock()
    monkeypatch.setattr(migration, "op", operations)
    monkeypatch.setattr(migration, "_column_names", lambda: set())

    migration.upgrade()

    assert migration.revision == "0064_cycle_receipt_monitoring"
    assert migration.down_revision == "0063_cycle_schedule_bindings"
    column = operations.add_column.call_args.args[1]
    assert column.name == "receipt_monitoring_started_at"
    assert column.nullable is False


def test_exact_cron_boundary_is_included_in_latest_slot():
    boundary = datetime(2026, 8, 17, 9, 0, tzinfo=timezone.utc)

    assert compute_latest_run_at(
        "0 9 * * *",
        "UTC",
        at_or_before=boundary,
    ) == boundary


def test_forward_and_backward_slots_agree_across_dst_transition():
    before_spring_forward = datetime(2026, 3, 7, 14, 0, tzinfo=timezone.utc)
    after_spring_forward = datetime(2026, 3, 8, 13, 0, tzinfo=timezone.utc)

    next_slot = compute_next_run_at(
        "0 9 * * *",
        "America/Toronto",
        from_dt=before_spring_forward,
    )
    latest_slot = compute_latest_run_at(
        "0 9 * * *",
        "America/Toronto",
        at_or_before=after_spring_forward,
    )

    assert next_slot == after_spring_forward
    assert latest_slot == next_slot
