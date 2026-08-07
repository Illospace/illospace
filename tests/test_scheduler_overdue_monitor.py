from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from brain.app.scheduler.daemon import async_scheduler_health_snapshot
from brain.app.scheduler.overdue_alert_state import (
    SCHEDULER_OVERDUE_FREEZE_ALERT_KEY,
    release_scheduler_alert,
    try_claim_scheduler_alert,
)
from brain.app.scheduler.overdue_monitor import (
    SchedulerOverdueMonitor,
    _SchedulerOverdueAlertState,
    _try_claim_scheduler_overdue_alert,
)
from brain.app.scheduler.read_models import (
    SchedulerOverdueCandidate,
    async_scheduler_overdue_candidates,
)
from brain.platform.db.models.scheduler import (
    SchedulerAlertLatch,
    SchedulerLease,
    SchedulerRun,
)
from tests.scheduler_test_support import (
    make_scheduler_job,
    make_scheduler_test_session,
)


NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)


class _FakeAlertLatch:
    def __init__(self) -> None:
        self.state: _SchedulerOverdueAlertState | None = None

    @property
    def claimed(self) -> bool:
        return self.state is not None

    async def claim(
        self,
        *,
        alerted_at: datetime,
        freeze_started_at: datetime,
        next_alert_at: datetime,
    ) -> bool:
        if self.state is not None and not (
            self.state.freeze_started_at is None
            or (
                self.state.freeze_started_at == freeze_started_at
                and (
                    self.state.next_alert_at is None
                    or self.state.next_alert_at <= alerted_at
                )
            )
        ):
            return False
        self.state = _SchedulerOverdueAlertState(
            alerted_at=alerted_at,
            freeze_started_at=freeze_started_at,
            next_alert_at=next_alert_at,
        )
        return True

    async def release(self) -> _SchedulerOverdueAlertState | None:
        released = self.state
        self.state = None
        return released


def _monitor(
    *,
    alert_latch: _FakeAlertLatch | None = None,
    **kwargs,
) -> SchedulerOverdueMonitor:
    latch = alert_latch or _FakeAlertLatch()
    kwargs.setdefault("claim_alert", latch.claim)
    kwargs.setdefault("release_alert", latch.release)
    return SchedulerOverdueMonitor(**kwargs)


@pytest.fixture
async def scheduler_session(async_sqlite_session_factory):
    return await make_scheduler_test_session(async_sqlite_session_factory)


def _candidate(
    *,
    now: datetime,
    lag_seconds: int,
    job_key: str = "uwear_aws_health_scan",
) -> SchedulerOverdueCandidate:
    return SchedulerOverdueCandidate(
        job_key=job_key,
        next_run_at=now - timedelta(seconds=lag_seconds),
    )


def test_overdue_monitor_checks_well_inside_twenty_minute_window():
    assert SchedulerOverdueMonitor.check_interval_seconds <= 5 * 60


def test_overdue_monitor_is_constructible_without_arguments():
    assert SchedulerOverdueMonitor().name == "scheduler_overdue_monitor"


@pytest.mark.parametrize(
    ("elapsed", "expected_next"),
    (
        (timedelta(minutes=18), timedelta(hours=1)),
        (timedelta(hours=1), timedelta(hours=4)),
        (timedelta(hours=4), timedelta(hours=12)),
        (timedelta(hours=12), timedelta(hours=24)),
        (timedelta(hours=24), timedelta(hours=36)),
    ),
)
def test_escalation_schedule_repeats_every_twelve_hours(
    elapsed,
    expected_next,
):
    monitor = SchedulerOverdueMonitor()

    assert monitor._next_escalation_at(
        freeze_started_at=NOW,
        now=NOW + elapsed,
    ) == NOW + expected_next


async def test_job_overdue_by_more_than_fifteen_minutes_alerts_with_tick_time():
    deliveries = []
    last_tick_at = NOW - timedelta(minutes=16)

    async def candidates(_session, *, now):
        return (_candidate(now=now, lag_seconds=16 * 60),)

    async def liveness_checkpoint(_session):
        return last_tick_at

    async def deliver_alert(**kwargs):
        deliveries.append(kwargs)

    monitor = _monitor(
        candidate_provider=candidates,
        liveness_checkpoint=liveness_checkpoint,
        deliver_alert=deliver_alert,
    )
    result = await monitor._check(object(), now=NOW)

    assert monitor.name == "scheduler_overdue_monitor"
    assert result.alert_sent is True
    assert result.overdue_job_keys == ("uwear_aws_health_scan",)
    assert len(deliveries) == 1
    alert = deliveries[0]
    assert alert["policy"].requested_by == "scheduler_overdue_monitor"
    assert alert["subject"].identity == "uwear_aws_health_scan"
    assert "16m overdue" in alert["presentation"].summary
    assert (
        "Daemon has not ticked for 16m "
        f"(last tick {last_tick_at.isoformat()})."
    ) in alert["presentation"].summary


async def test_five_hour_freeze_alerts_at_duration_thresholds():
    deliveries = []
    freeze_started_at = NOW

    async def candidates(_session, *, now):
        return (_candidate(now=now, lag_seconds=16 * 60),)

    async def liveness_checkpoint(_session):
        return freeze_started_at

    async def deliver_alert(**kwargs):
        deliveries.append(kwargs)

    monitor = _monitor(
        candidate_provider=candidates,
        liveness_checkpoint=liveness_checkpoint,
        deliver_alert=deliver_alert,
    )
    for elapsed_minutes in range(18, 5 * 60 + 1):
        await monitor._check(
            object(),
            now=freeze_started_at + timedelta(minutes=elapsed_minutes),
        )

    assert [
        delivery["presentation"].summary.splitlines()[-1]
        for delivery in deliveries
    ] == [
        "Daemon has not ticked for 18m "
        f"(last tick {freeze_started_at.isoformat()}).",
        "Daemon has not ticked for 1h "
        f"(last tick {freeze_started_at.isoformat()}).",
        "Daemon has not ticked for 4h "
        f"(last tick {freeze_started_at.isoformat()}).",
    ]


async def test_all_jobs_on_schedule_for_twenty_four_hours_produces_no_alert():
    deliveries = []

    async def candidates(_session, *, now):
        return (_candidate(now=now, lag_seconds=0),)

    async def liveness_checkpoint(_session):
        return NOW

    async def deliver_alert(**kwargs):
        deliveries.append(kwargs)

    monitor = _monitor(
        candidate_provider=candidates,
        liveness_checkpoint=liveness_checkpoint,
        deliver_alert=deliver_alert,
    )
    for hour in range(25):
        result = await monitor._check(object(), now=NOW + timedelta(hours=hour))
        assert result.alert_sent is False
        assert result.overdue_job_keys == ()
    assert deliveries == []


async def test_alert_fires_once_per_freeze_and_rearms_after_recovery():
    deliveries = []
    lag_seconds = 60 * 60

    async def candidates(_session, *, now):
        return (_candidate(now=now, lag_seconds=lag_seconds),)

    async def liveness_checkpoint(_session):
        return NOW

    async def deliver_alert(**kwargs):
        deliveries.append(kwargs)

    monitor = _monitor(
        candidate_provider=candidates,
        liveness_checkpoint=liveness_checkpoint,
        deliver_alert=deliver_alert,
    )
    first = await monitor._check(object(), now=NOW)
    repeated = await monitor._check(object(), now=NOW + timedelta(minutes=1))
    lag_seconds = 0
    recovered = await monitor._check(object(), now=NOW + timedelta(minutes=2))
    lag_seconds = 60 * 60
    later_freeze = await monitor._check(object(), now=NOW + timedelta(hours=2))

    assert first.alert_sent is True
    assert repeated.alert_sent is False
    assert recovered.alert_sent is False
    assert later_freeze.alert_sent is True
    assert [
        delivery["presentation"].title for delivery in deliveries
    ] == [
        "Scheduler jobs overdue",
        "Scheduler jobs recovered",
        "Scheduler jobs overdue",
    ]


async def test_twenty_minute_freeze_has_one_alert_and_one_recovery():
    deliveries = []
    lag_seconds = 16 * 60
    last_tick_at = NOW

    async def candidates(_session, *, now):
        return (_candidate(now=now, lag_seconds=lag_seconds),)

    async def liveness_checkpoint(_session):
        return last_tick_at

    async def deliver_alert(**kwargs):
        deliveries.append(kwargs)

    monitor = _monitor(
        candidate_provider=candidates,
        liveness_checkpoint=liveness_checkpoint,
        deliver_alert=deliver_alert,
    )
    for elapsed_minutes in range(16, 20):
        await monitor._check(
            object(),
            now=NOW + timedelta(minutes=elapsed_minutes),
        )

    lag_seconds = 0
    last_tick_at = NOW + timedelta(minutes=20)
    await monitor._check(object(), now=last_tick_at)

    assert [
        delivery["presentation"].title for delivery in deliveries
    ] == ["Scheduler jobs overdue", "Scheduler jobs recovered"]
    assert (
        deliveries[0]["presentation"].summary.splitlines()[-1]
        == "Daemon has not ticked for 16m "
        f"(last tick {NOW.isoformat()})."
    )
    assert (
        deliveries[1]["presentation"].summary
        == f"Daemon resumed ticking at {last_tick_at.isoformat()} "
        "after 20m without a tick."
    )


async def test_failed_alert_delivery_releases_claim_and_retries_next_tick():
    alert_latch = _FakeAlertLatch()
    delivery_attempts = 0
    successful_deliveries = []
    release_calls = 0

    async def candidates(_session, *, now):
        return (_candidate(now=now, lag_seconds=60 * 60),)

    async def liveness_checkpoint(_session):
        return NOW

    async def release_alert():
        nonlocal release_calls
        release_calls += 1
        await alert_latch.release()

    async def deliver_alert(**kwargs):
        nonlocal delivery_attempts
        delivery_attempts += 1
        if delivery_attempts == 1:
            raise RuntimeError("Slack delivery failed")
        successful_deliveries.append(kwargs)

    monitor = _monitor(
        alert_latch=alert_latch,
        candidate_provider=candidates,
        liveness_checkpoint=liveness_checkpoint,
        release_alert=release_alert,
        deliver_alert=deliver_alert,
    )

    with pytest.raises(RuntimeError, match="Slack delivery failed"):
        await monitor._check(object(), now=NOW)

    assert release_calls == 1
    assert alert_latch.claimed is False

    retry = await monitor._check(object(), now=NOW + timedelta(minutes=1))

    assert retry.alert_sent is True
    assert delivery_attempts == 2
    assert len(successful_deliveries) == 1


async def test_api_restart_mid_freeze_uses_the_existing_scheduler_claim():
    deliveries = []
    alert_latch = _FakeAlertLatch()

    async def candidates(_session, *, now):
        return (_candidate(now=now, lag_seconds=60 * 60),)

    async def liveness_checkpoint(_session):
        return NOW

    async def deliver_alert(**kwargs):
        deliveries.append(kwargs)

    first_monitor = _monitor(
        alert_latch=alert_latch,
        candidate_provider=candidates,
        liveness_checkpoint=liveness_checkpoint,
        deliver_alert=deliver_alert,
    )
    restarted_monitor = _monitor(
        alert_latch=alert_latch,
        candidate_provider=candidates,
        liveness_checkpoint=liveness_checkpoint,
        deliver_alert=deliver_alert,
    )

    first = await first_monitor._check(object(), now=NOW)
    after_restart = await restarted_monitor._check(
        object(),
        now=NOW + timedelta(minutes=1),
    )

    assert first.alert_sent is True
    assert after_restart.alert_sent is False
    assert len(deliveries) == 1


async def test_two_replicas_deliver_only_after_winning_the_atomic_claim():
    deliveries = []
    claim_results = iter((True, False))

    async def candidates(_session, *, now):
        return (_candidate(now=now, lag_seconds=60 * 60),)

    async def liveness_checkpoint(_session):
        return NOW

    async def claim_alert(
        *,
        alerted_at,
        freeze_started_at,
        next_alert_at,
    ):
        assert alerted_at == NOW
        assert freeze_started_at == NOW
        assert next_alert_at == NOW + timedelta(hours=1)
        return next(claim_results)

    async def release_alert():
        raise AssertionError("an overdue observation must not release the claim")

    async def deliver_alert(**kwargs):
        deliveries.append(kwargs)

    replicas = tuple(
        _monitor(
            candidate_provider=candidates,
            liveness_checkpoint=liveness_checkpoint,
            claim_alert=claim_alert,
            release_alert=release_alert,
            deliver_alert=deliver_alert,
        )
        for _ in range(2)
    )

    results = await asyncio.gather(
        *(replica._check(object(), now=NOW) for replica in replicas)
    )

    assert [result.alert_sent for result in results].count(True) == 1
    assert len(deliveries) == 1


async def test_scheduler_alert_claim_is_atomic_and_can_rearm(scheduler_session):
    first = await try_claim_scheduler_alert(
        scheduler_session,
        alert_key=SCHEDULER_OVERDUE_FREEZE_ALERT_KEY,
        alerted_at=NOW,
    )
    repeated = await try_claim_scheduler_alert(
        scheduler_session,
        alert_key=SCHEDULER_OVERDUE_FREEZE_ALERT_KEY,
        alerted_at=NOW + timedelta(minutes=1),
    )

    latch = await scheduler_session.get(
        SchedulerAlertLatch,
        SCHEDULER_OVERDUE_FREEZE_ALERT_KEY,
    )
    assert first is True
    assert repeated is False
    assert latch is not None
    assert latch.alert_key == SCHEDULER_OVERDUE_FREEZE_ALERT_KEY
    assert latch.alerted_at.replace(tzinfo=timezone.utc) == NOW

    await release_scheduler_alert(
        scheduler_session,
        alert_key=SCHEDULER_OVERDUE_FREEZE_ALERT_KEY,
    )
    later_freeze = await try_claim_scheduler_alert(
        scheduler_session,
        alert_key=SCHEDULER_OVERDUE_FREEZE_ALERT_KEY,
        alerted_at=NOW + timedelta(hours=2),
    )

    assert later_freeze is True


async def test_scheduler_escalation_claim_advances_one_durable_row(
    scheduler_session,
):
    first = await _try_claim_scheduler_overdue_alert(
        scheduler_session,
        alerted_at=NOW + timedelta(minutes=18),
        freeze_started_at=NOW,
        next_alert_at=NOW + timedelta(hours=1),
    )
    premature = await _try_claim_scheduler_overdue_alert(
        scheduler_session,
        alerted_at=NOW + timedelta(minutes=59),
        freeze_started_at=NOW,
        next_alert_at=NOW + timedelta(hours=1),
    )
    escalation = await _try_claim_scheduler_overdue_alert(
        scheduler_session,
        alerted_at=NOW + timedelta(hours=1),
        freeze_started_at=NOW,
        next_alert_at=NOW + timedelta(hours=4),
    )
    repeated = await _try_claim_scheduler_overdue_alert(
        scheduler_session,
        alerted_at=NOW + timedelta(hours=1),
        freeze_started_at=NOW,
        next_alert_at=NOW + timedelta(hours=4),
    )

    latch = await scheduler_session.get(
        SchedulerAlertLatch,
        SCHEDULER_OVERDUE_FREEZE_ALERT_KEY,
    )
    assert [first, premature, escalation, repeated] == [True, False, True, False]
    assert latch is not None
    assert latch.freeze_started_at.replace(tzinfo=timezone.utc) == NOW
    assert latch.alerted_at.replace(tzinfo=timezone.utc) == NOW + timedelta(hours=1)
    assert latch.next_alert_at.replace(tzinfo=timezone.utc) == NOW + timedelta(hours=4)
    assert len(
        (
            await scheduler_session.execute(
                SchedulerAlertLatch.__table__.select().where(
                    SchedulerAlertLatch.alert_key
                    == SCHEDULER_OVERDUE_FREEZE_ALERT_KEY
                )
            )
        ).all()
    ) == 1


@pytest.mark.requires_db
async def test_postgres_concurrent_scheduler_alert_claim_has_one_winner(db_engine):
    alert_key = f"scheduler_overdue_freeze_test_{uuid.uuid4().hex}"

    async def claim() -> bool:
        async with AsyncSession(bind=db_engine, expire_on_commit=False) as session:
            won = await try_claim_scheduler_alert(
                session,
                alert_key=alert_key,
                alerted_at=NOW,
            )
            await session.commit()
            return won

    try:
        assert sorted(await asyncio.gather(claim(), claim())) == [False, True]
    finally:
        async with AsyncSession(bind=db_engine) as session:
            await release_scheduler_alert(session, alert_key=alert_key)
            await session.commit()


@pytest.mark.requires_db
async def test_postgres_concurrent_escalation_claim_has_one_winner(db_engine):
    async with AsyncSession(bind=db_engine, expire_on_commit=False) as session:
        await release_scheduler_alert(
            session,
            alert_key=SCHEDULER_OVERDUE_FREEZE_ALERT_KEY,
        )
        assert await _try_claim_scheduler_overdue_alert(
            session,
            alerted_at=NOW + timedelta(minutes=18),
            freeze_started_at=NOW,
            next_alert_at=NOW + timedelta(hours=1),
        )
        await session.commit()

    async def escalate() -> bool:
        async with AsyncSession(bind=db_engine, expire_on_commit=False) as session:
            won = await _try_claim_scheduler_overdue_alert(
                session,
                alerted_at=NOW + timedelta(hours=1),
                freeze_started_at=NOW,
                next_alert_at=NOW + timedelta(hours=4),
            )
            await session.commit()
            return won

    try:
        assert sorted(await asyncio.gather(escalate(), escalate())) == [False, True]
        async with AsyncSession(bind=db_engine, expire_on_commit=False) as session:
            latch = await session.get(
                SchedulerAlertLatch,
                SCHEDULER_OVERDUE_FREEZE_ALERT_KEY,
            )
            assert latch is not None
            assert latch.alerted_at == NOW + timedelta(hours=1)
            assert latch.next_alert_at == NOW + timedelta(hours=4)
    finally:
        async with AsyncSession(bind=db_engine) as session:
            await release_scheduler_alert(
                session,
                alert_key=SCHEDULER_OVERDUE_FREEZE_ALERT_KEY,
            )
            await session.commit()


def test_scheduler_alert_latch_has_no_job_owner_and_names_its_timestamp():
    table = SchedulerAlertLatch.__table__

    assert set(table.columns.keys()) == {
        "alert_key",
        "alerted_at",
        "freeze_started_at",
        "next_alert_at",
    }
    assert table.columns["alert_key"].type.length == 80
    assert table.columns["alerted_at"].nullable is False
    assert table.columns["freeze_started_at"].nullable is True
    assert table.columns["next_alert_at"].nullable is True
    assert not table.foreign_keys


async def test_multiple_overdue_jobs_produce_one_freeze_alert():
    deliveries = []

    async def candidates(_session, *, now):
        return (
            _candidate(now=now, lag_seconds=60 * 60),
            _candidate(
                now=now,
                lag_seconds=45 * 60,
                job_key="knowledge_index_sync",
            ),
        )

    async def liveness_checkpoint(_session):
        return NOW - timedelta(hours=1)

    async def deliver_alert(**kwargs):
        deliveries.append(kwargs)

    monitor = _monitor(
        candidate_provider=candidates,
        liveness_checkpoint=liveness_checkpoint,
        deliver_alert=deliver_alert,
    )
    result = await monitor._check(object(), now=NOW)

    assert result.overdue_job_keys == (
        "uwear_aws_health_scan",
        "knowledge_index_sync",
    )
    assert len(deliveries) == 1
    assert "uwear_aws_health_scan" in deliveries[0]["presentation"].summary
    assert "knowledge_index_sync" in deliveries[0]["presentation"].summary


async def test_fresh_daemon_heartbeat_does_not_hide_stale_next_run_at():
    deliveries = []

    async def candidates(_session, *, now):
        return (_candidate(now=now, lag_seconds=60 * 60),)

    async def liveness_checkpoint(_session):
        return NOW

    async def deliver_alert(**kwargs):
        deliveries.append(kwargs)

    monitor = _monitor(
        candidate_provider=candidates,
        liveness_checkpoint=liveness_checkpoint,
        deliver_alert=deliver_alert,
    )
    result = await monitor._check(object(), now=NOW)

    assert result.alert_sent is True
    assert result.last_tick_at == NOW
    assert len(deliveries) == 1


async def test_slack_delivery_starts_after_read_transaction_closes():
    transaction_open = False

    class FakeUnitOfWork:
        session = object()

        async def __aenter__(self):
            nonlocal transaction_open
            transaction_open = True
            return self

        async def __aexit__(self, *_exc):
            nonlocal transaction_open
            transaction_open = False

    async def candidates(_session, *, now):
        assert transaction_open is True
        return (_candidate(now=now, lag_seconds=60 * 60),)

    async def liveness_checkpoint(_session):
        assert transaction_open is True
        return NOW

    async def try_claim(
        _session,
        *,
        alerted_at,
        freeze_started_at,
        next_alert_at,
    ):
        assert alerted_at == NOW
        assert freeze_started_at == NOW
        assert next_alert_at == NOW + timedelta(hours=1)
        assert transaction_open is True
        return True

    async def deliver_alert(**_kwargs):
        assert transaction_open is False

    monitor = SchedulerOverdueMonitor(
        candidate_provider=candidates,
        liveness_checkpoint=liveness_checkpoint,
        deliver_alert=deliver_alert,
    )
    with patch(
        "brain.platform.db.repositories.unit_of_work.UnitOfWork",
        FakeUnitOfWork,
    ), patch(
        "brain.app.scheduler.overdue_monitor._try_claim_scheduler_overdue_alert",
        try_claim,
    ):
        result = await monitor._run_once(now=NOW)

    assert result.alert_sent is True


async def test_monitor_projection_selects_only_eligible_scheduler_jobs(
    scheduler_session,
):
    scheduler_session.add_all(
        [
            make_scheduler_job(
                job_key="eligible",
                family="eligible",
                program_key="eligible",
                next_run_at=NOW - timedelta(hours=1),
            ),
            make_scheduler_job(
                job_key="disabled",
                family="disabled",
                program_key="disabled",
                enabled=False,
                next_run_at=NOW - timedelta(hours=1),
            ),
            make_scheduler_job(
                job_key="paused",
                family="paused",
                program_key="paused",
                pause_reason="operator pause",
                next_run_at=NOW - timedelta(hours=1),
            ),
            make_scheduler_job(
                job_key="future",
                family="future",
                program_key="future",
                next_run_at=NOW + timedelta(minutes=1),
            ),
            make_scheduler_job(
                job_key="cron_owned",
                family="cron_owned",
                program_key="cron_owned",
                owner_mode="cron",
                next_run_at=NOW - timedelta(hours=1),
            ),
        ]
    )
    await scheduler_session.flush()

    candidates = await async_scheduler_overdue_candidates(
        scheduler_session,
        now=NOW,
    )

    assert tuple(candidate.job_key for candidate in candidates) == ("eligible",)


async def test_monitor_skips_active_job_while_health_reports_canonical_lag(
    scheduler_session,
):
    job = make_scheduler_job(
        job_key="long_running_job",
        family="long_running_job",
        program_key="long_running_job",
        next_run_at=NOW - timedelta(hours=1),
    )
    scheduler_session.add(job)
    await scheduler_session.flush()
    run = SchedulerRun(
        job_id=job.id,
        scheduled_for=NOW - timedelta(hours=2),
        window_start=NOW - timedelta(hours=2),
        window_end=NOW - timedelta(hours=2),
        status="running",
        idempotency_key="long_running_job:2026-08-05T10:00:00+00:00",
        started_at=NOW - timedelta(hours=2),
    )
    scheduler_session.add(run)
    await scheduler_session.flush()
    lease = SchedulerLease(
        run_id=run.id,
        owner_id="scheduler-worker",
        owner_host="scheduler-host",
        owner_pid=42,
        acquired_at=NOW - timedelta(minutes=1),
        heartbeat_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
    )
    scheduler_session.add(lease)
    await scheduler_session.flush()
    run.lease_id = lease.id
    await scheduler_session.flush()

    snapshot = await async_scheduler_health_snapshot(scheduler_session, now=NOW)
    deliveries = []

    async def liveness_checkpoint(_session):
        return NOW

    async def deliver_alert(**kwargs):
        deliveries.append(kwargs)

    monitor = _monitor(
        liveness_checkpoint=liveness_checkpoint,
        deliver_alert=deliver_alert,
    )
    result = await monitor._check(scheduler_session, now=NOW)

    assert [item["job_key"] for item in snapshot["lag"]["lagging_jobs"]] == [
        "long_running_job"
    ]
    assert snapshot["health"]["status"] == "degraded"
    assert result.alert_sent is False
    assert result.overdue_job_keys == ()
    assert deliveries == []


async def test_scheduler_global_latch_is_independent_of_job_identity(
    scheduler_session,
):
    job = make_scheduler_job(
        job_key="stalled_job",
        family="stalled_job",
        program_key="stalled_job",
        next_run_at=NOW - timedelta(hours=1),
    )
    scheduler_session.add(job)
    await scheduler_session.flush()
    deliveries = []

    async def liveness_checkpoint(_session):
        return NOW

    async def deliver_alert(**kwargs):
        deliveries.append(kwargs)

    monitor = _monitor(
        liveness_checkpoint=liveness_checkpoint,
        deliver_alert=deliver_alert,
    )
    assert (await monitor._check(scheduler_session, now=NOW)).alert_sent is True

    await scheduler_session.delete(job)
    replacement = make_scheduler_job(
        job_key="replacement_stalled_job",
        family="replacement_stalled_job",
        program_key="replacement_stalled_job",
        next_run_at=NOW - timedelta(hours=1),
    )
    scheduler_session.add(replacement)
    await scheduler_session.flush()
    assert (
        await monitor._check(scheduler_session, now=NOW + timedelta(minutes=1))
    ).alert_sent is False

    replacement.next_run_at = NOW + timedelta(minutes=5)
    await scheduler_session.flush()
    assert (
        await monitor._check(scheduler_session, now=NOW + timedelta(minutes=2))
    ).alert_sent is False
    replacement.next_run_at = NOW - timedelta(hours=1)
    await scheduler_session.flush()
    assert (
        await monitor._check(scheduler_session, now=NOW + timedelta(hours=2))
    ).alert_sent is True
    assert [
        delivery["presentation"].title for delivery in deliveries
    ] == [
        "Scheduler jobs overdue",
        "Scheduler jobs recovered",
        "Scheduler jobs overdue",
    ]
