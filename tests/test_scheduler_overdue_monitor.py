from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from brain.app.scheduler import overdue_monitor as overdue_monitor_module
from brain.app.scheduler.daemon import async_scheduler_health_snapshot
from brain.app.scheduler.overdue_alert_state import (
    SCHEDULER_OVERDUE_FREEZE_ALERT_KEY,
    SchedulerOverdueAlertState,
    SchedulerSelfHealState,
    claim_scheduler_self_heal,
    release_scheduler_alert,
    release_scheduler_overdue_alert,
    try_claim_scheduler_alert,
    try_claim_scheduler_overdue_alert,
    try_claim_scheduler_self_heal,
)
from brain.app.scheduler.overdue_monitor import (
    SchedulerOverdueMonitor,
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
        self.state: SchedulerOverdueAlertState | None = None

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
        self.state = SchedulerOverdueAlertState(
            alerted_at=alerted_at,
            freeze_started_at=freeze_started_at,
            next_alert_at=next_alert_at,
        )
        return True

    async def release(self) -> SchedulerOverdueAlertState | None:
        released = self.state
        self.state = None
        return released


def _monitor(
    *,
    alert_latch: _FakeAlertLatch | None = None,
    **kwargs,
) -> SchedulerOverdueMonitor:
    latch = alert_latch or _FakeAlertLatch()

    async def no_self_heal(**_kwargs):
        return SchedulerSelfHealState(
            attempt=None,
            attempts=0,
            exhausted=False,
        )

    async def confirm_freeze(**kwargs):
        return kwargs.get("freeze_started_at") is not None

    kwargs.setdefault("claim_alert", latch.claim)
    kwargs.setdefault("release_alert", latch.release)
    kwargs.setdefault("claim_self_heal", no_self_heal)
    kwargs.setdefault("confirm_freeze", confirm_freeze)
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


def test_self_heal_defaults_and_environment_overrides(monkeypatch):
    monitor = SchedulerOverdueMonitor()

    assert monitor.self_heal_after == timedelta(minutes=10)
    assert monitor.self_heal_max_attempts == 2

    monkeypatch.setenv("SCHEDULER_SELF_HEAL_AFTER_MINUTES", "7")
    monkeypatch.setenv("SCHEDULER_SELF_HEAL_MAX_ATTEMPTS", "3")
    configured = SchedulerOverdueMonitor()

    assert configured.self_heal_after == timedelta(minutes=7)
    assert configured.self_heal_max_attempts == 3


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
        return (
            SchedulerOverdueCandidate(
                job_key="uwear_aws_health_scan",
                next_run_at=freeze_started_at - timedelta(minutes=15),
            ),
        )

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
        == f"Scheduler jobs advanced at {last_tick_at.isoformat()} "
        "after an overdue freeze of 5m."
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
        assert freeze_started_at == NOW - timedelta(minutes=45)
        assert next_alert_at == NOW + timedelta(minutes=15)
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
    first = await try_claim_scheduler_overdue_alert(
        scheduler_session,
        alerted_at=NOW + timedelta(minutes=18),
        freeze_started_at=NOW,
        next_alert_at=NOW + timedelta(hours=1),
    )
    premature = await try_claim_scheduler_overdue_alert(
        scheduler_session,
        alerted_at=NOW + timedelta(minutes=59),
        freeze_started_at=NOW,
        next_alert_at=NOW + timedelta(hours=1),
    )
    escalation = await try_claim_scheduler_overdue_alert(
        scheduler_session,
        alerted_at=NOW + timedelta(hours=1),
        freeze_started_at=NOW,
        next_alert_at=NOW + timedelta(hours=4),
    )
    repeated = await try_claim_scheduler_overdue_alert(
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


async def test_scheduler_self_heal_claim_caps_and_rearms_by_episode(
    scheduler_session,
):
    first = await try_claim_scheduler_self_heal(
        scheduler_session,
        attempted_at=NOW,
        freeze_started_at=NOW - timedelta(minutes=20),
        next_attempt_at=NOW + timedelta(minutes=10),
        max_attempts=2,
    )
    repeated = await try_claim_scheduler_self_heal(
        scheduler_session,
        attempted_at=NOW + timedelta(minutes=1),
        freeze_started_at=NOW - timedelta(minutes=20),
        next_attempt_at=NOW + timedelta(minutes=11),
        max_attempts=2,
    )
    second = await try_claim_scheduler_self_heal(
        scheduler_session,
        attempted_at=NOW + timedelta(minutes=10),
        freeze_started_at=NOW - timedelta(minutes=20),
        next_attempt_at=NOW + timedelta(minutes=20),
        max_attempts=2,
    )
    exhausted = await try_claim_scheduler_self_heal(
        scheduler_session,
        attempted_at=NOW + timedelta(minutes=20),
        freeze_started_at=NOW - timedelta(minutes=20),
        next_attempt_at=NOW + timedelta(minutes=30),
        max_attempts=2,
    )
    next_episode = await try_claim_scheduler_self_heal(
        scheduler_session,
        attempted_at=NOW + timedelta(hours=2),
        freeze_started_at=NOW + timedelta(hours=1),
        next_attempt_at=NOW + timedelta(hours=2, minutes=10),
        max_attempts=2,
    )

    assert first.attempt == 1
    assert repeated == SchedulerSelfHealState(
        attempt=None,
        attempts=1,
        exhausted=False,
        freeze_started_at=NOW - timedelta(minutes=20),
    )
    assert second.attempt == 2
    assert exhausted.attempt is None
    assert exhausted.attempts == 2
    assert exhausted.exhausted is True
    assert next_episode.attempt == 1
    assert next_episode.attempts == 1


async def test_committed_self_heal_claim_uses_old_durable_freeze_latch(
    scheduler_session,
):
    freeze_started_at = NOW - timedelta(minutes=20)
    scheduler_session.add(
        SchedulerAlertLatch(
            alert_key=SCHEDULER_OVERDUE_FREEZE_ALERT_KEY,
            alerted_at=freeze_started_at,
            freeze_started_at=freeze_started_at,
            next_alert_at=NOW + timedelta(hours=1),
        )
    )
    await scheduler_session.flush()

    class FakeUnitOfWork:
        session = scheduler_session

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

    with patch(
        "brain.platform.db.repositories.unit_of_work.UnitOfWork",
        FakeUnitOfWork,
    ):
        claimed = await claim_scheduler_self_heal(
            attempted_at=NOW,
            heal_after=timedelta(minutes=10),
            max_attempts=2,
        )

    assert claimed.attempt == 1
    assert claimed.freeze_started_at == freeze_started_at


async def test_releasing_overdue_latch_returns_durable_self_heal_attempts(
    scheduler_session,
):
    freeze_started_at = NOW - timedelta(minutes=20)
    scheduler_session.add_all(
        [
            SchedulerAlertLatch(
                alert_key=SCHEDULER_OVERDUE_FREEZE_ALERT_KEY,
                alerted_at=freeze_started_at,
                freeze_started_at=freeze_started_at,
                next_alert_at=NOW + timedelta(hours=1),
            ),
            SchedulerAlertLatch(
                alert_key="scheduler_self_heal",
                alerted_at=NOW - timedelta(minutes=10),
                freeze_started_at=freeze_started_at,
                next_alert_at=NOW,
                attempt_count=2,
            ),
        ]
    )
    await scheduler_session.flush()

    class FakeUnitOfWork:
        session = scheduler_session

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

    with patch(
        "brain.platform.db.repositories.unit_of_work.UnitOfWork",
        FakeUnitOfWork,
    ):
        released = await release_scheduler_overdue_alert()

    assert released is not None
    assert released.self_heal_attempts == 2
    assert (
        await scheduler_session.get(
            SchedulerAlertLatch,
            "scheduler_self_heal",
        )
        is not None
    )


async def test_overdue_monitor_queues_one_self_heal_request_and_alert():
    deliveries = []
    restart_calls = []
    claims = iter(
        (
            SchedulerSelfHealState(
                attempt=1,
                attempts=1,
                exhausted=False,
                freeze_started_at=NOW - timedelta(minutes=20),
            ),
            SchedulerSelfHealState(
                attempt=None,
                attempts=1,
                exhausted=False,
                freeze_started_at=NOW - timedelta(minutes=20),
            ),
        )
    )

    async def candidates(_session, *, now):
        return (_candidate(now=now, lag_seconds=60 * 60),)

    async def liveness_checkpoint(_session):
        return NOW

    async def claim_self_heal(**_kwargs):
        return next(claims)

    async def restart_runtime_services(services, *, requested_by):
        restart_calls.append((services, requested_by))
        return True

    async def deliver_alert(**kwargs):
        deliveries.append(kwargs)

    monitor = _monitor(
        candidate_provider=candidates,
        liveness_checkpoint=liveness_checkpoint,
        claim_self_heal=claim_self_heal,
        restart_runtime_services=restart_runtime_services,
        deliver_alert=deliver_alert,
    )

    first = await monitor._check(object(), now=NOW)
    repeated = await monitor._check(object(), now=NOW + timedelta(minutes=1))

    assert first.self_heal_attempt == 1
    assert repeated.self_heal_attempt is None
    assert restart_calls == [(["scheduler"], "scheduler-self-heal")]
    self_heal_alert = next(
        item
        for item in deliveries
        if item["presentation"].title == "Scheduler self-heal"
    )
    assert self_heal_alert["presentation"].summary == (
        "Restarting scheduler automatically, freeze 20m, attempt 1/2."
    )


async def test_exhausted_self_heal_marks_next_escalation_for_a_human():
    deliveries = []

    async def candidates(_session, *, now):
        return (_candidate(now=now, lag_seconds=60 * 60),)

    async def liveness_checkpoint(_session):
        return NOW - timedelta(hours=1)

    async def exhausted_self_heal(**_kwargs):
        return SchedulerSelfHealState(
            attempt=None,
            attempts=2,
            exhausted=True,
            freeze_started_at=NOW - timedelta(hours=1),
        )

    async def deliver_alert(**kwargs):
        deliveries.append(kwargs)

    monitor = _monitor(
        candidate_provider=candidates,
        liveness_checkpoint=liveness_checkpoint,
        claim_self_heal=exhausted_self_heal,
        deliver_alert=deliver_alert,
    )

    await monitor._check(object(), now=NOW)

    summary = deliveries[0]["presentation"].summary
    assert "Self-heal failed after 2 attempts" in summary
    assert "a human is needed" in summary


async def test_failed_restart_write_consumes_attempt_and_preserves_escalation():
    deliveries = []

    async def candidates(_session, *, now):
        return (_candidate(now=now, lag_seconds=60 * 60),)

    async def liveness_checkpoint(_session):
        return NOW

    async def final_self_heal_attempt(**_kwargs):
        return SchedulerSelfHealState(
            attempt=2,
            attempts=2,
            exhausted=False,
            freeze_started_at=NOW - timedelta(minutes=20),
        )

    async def restart_runtime_services(_services, *, requested_by):
        assert requested_by == "scheduler-self-heal"
        raise OSError("runtime queue is unavailable")

    async def deliver_alert(**kwargs):
        deliveries.append(kwargs)

    monitor = _monitor(
        candidate_provider=candidates,
        liveness_checkpoint=liveness_checkpoint,
        claim_self_heal=final_self_heal_attempt,
        restart_runtime_services=restart_runtime_services,
        deliver_alert=deliver_alert,
    )

    result = await monitor._check(object(), now=NOW)

    assert result.self_heal_attempt is None
    assert "Self-heal failed after 2 attempts" in (
        deliveries[0]["presentation"].summary
    )


async def test_recovered_scheduler_is_not_restarted_from_stale_observation():
    restart_calls = []
    refunded_attempts = []

    async def candidates(_session, *, now):
        return (_candidate(now=now, lag_seconds=60 * 60),)

    async def liveness_checkpoint(_session):
        return NOW

    async def claim_self_heal(**_kwargs):
        return SchedulerSelfHealState(
            attempt=1,
            attempts=1,
            exhausted=False,
            freeze_started_at=NOW - timedelta(minutes=45),
        )

    async def confirm_freeze(**_kwargs):
        return False

    async def release_self_heal_claim(**kwargs):
        refunded_attempts.append(kwargs)

    async def restart_runtime_services(*args, **kwargs):
        restart_calls.append((args, kwargs))
        return True

    async def deliver_alert(**_kwargs):
        return None

    monitor = _monitor(
        candidate_provider=candidates,
        liveness_checkpoint=liveness_checkpoint,
        claim_self_heal=claim_self_heal,
        confirm_freeze=confirm_freeze,
        release_self_heal_claim=release_self_heal_claim,
        restart_runtime_services=restart_runtime_services,
        deliver_alert=deliver_alert,
    )

    result = await monitor._check(object(), now=NOW)

    assert result.self_heal_attempt is None
    assert restart_calls == []
    assert refunded_attempts == [
        {
            "attempted_at": NOW,
            "freeze_started_at": NOW - timedelta(minutes=45),
            "attempt": 1,
        }
    ]


async def test_self_heal_confirmation_rejects_replaced_durable_episode(
    scheduler_session,
    monkeypatch,
):
    old_episode = NOW - timedelta(minutes=45)
    current_episode = NOW - timedelta(minutes=30)
    scheduler_session.add(
        SchedulerAlertLatch(
            alert_key=SCHEDULER_OVERDUE_FREEZE_ALERT_KEY,
            alerted_at=current_episode,
            freeze_started_at=current_episode,
            next_alert_at=NOW + timedelta(hours=1),
        )
    )
    await scheduler_session.flush()

    class FakeUnitOfWork:
        session = scheduler_session

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc):
            return None

    async def candidates(_session, *, now):
        return (_candidate(now=now, lag_seconds=60 * 60),)

    monkeypatch.setattr(
        overdue_monitor_module,
        "async_scheduler_overdue_candidates",
        candidates,
    )
    with patch(
        "brain.platform.db.repositories.unit_of_work.UnitOfWork",
        FakeUnitOfWork,
    ):
        stale = await overdue_monitor_module._async_scheduler_freeze_is_current(
            freeze_started_at=old_episode,
            now=NOW,
            overdue_after=timedelta(minutes=15),
        )
        current = await overdue_monitor_module._async_scheduler_freeze_is_current(
            freeze_started_at=current_episode,
            now=NOW,
            overdue_after=timedelta(minutes=15),
        )

    assert stale is False
    assert current is True


async def test_recovery_after_self_heal_reports_attempts_and_posts_one_line():
    deliveries = []
    alert_latch = _FakeAlertLatch()
    alert_latch.state = SchedulerOverdueAlertState(
        alerted_at=NOW - timedelta(minutes=20),
        freeze_started_at=NOW - timedelta(minutes=20),
        next_alert_at=NOW + timedelta(hours=1),
        self_heal_attempts=1,
    )

    async def candidates(_session, *, now):
        return ()

    async def liveness_checkpoint(_session):
        return NOW

    async def deliver_alert(**kwargs):
        deliveries.append(kwargs)

    monitor = _monitor(
        alert_latch=alert_latch,
        candidate_provider=candidates,
        liveness_checkpoint=liveness_checkpoint,
        deliver_alert=deliver_alert,
    )

    await monitor._check(object(), now=NOW)

    assert deliveries[0]["presentation"].summary == (
        "Scheduler recovered after automatic restart, freeze 20m, attempts 1/2."
    )


async def test_stale_healthy_observation_does_not_release_current_freeze():
    release_calls = 0

    async def candidates(_session, *, now):
        return ()

    async def liveness_checkpoint(_session):
        return NOW

    async def release_alert():
        nonlocal release_calls
        release_calls += 1
        return None

    async def confirm_freeze(**_kwargs):
        return True

    monitor = _monitor(
        candidate_provider=candidates,
        liveness_checkpoint=liveness_checkpoint,
        release_alert=release_alert,
        confirm_freeze=confirm_freeze,
    )

    await monitor._check(object(), now=NOW)

    assert release_calls == 0


async def test_recovery_restores_alert_when_slack_delivery_fails():
    alert_latch = _FakeAlertLatch()
    alert_latch.state = SchedulerOverdueAlertState(
        alerted_at=NOW - timedelta(minutes=20),
        freeze_started_at=NOW - timedelta(minutes=20),
        next_alert_at=NOW + timedelta(hours=1),
        self_heal_attempts=2,
    )

    async def candidates(_session, *, now):
        return ()

    async def liveness_checkpoint(_session):
        return NOW

    async def deliver_alert(**_kwargs):
        raise RuntimeError("Slack delivery failed")

    monitor = _monitor(
        alert_latch=alert_latch,
        candidate_provider=candidates,
        liveness_checkpoint=liveness_checkpoint,
        deliver_alert=deliver_alert,
    )

    with pytest.raises(RuntimeError, match="Slack delivery failed"):
        await monitor._check(object(), now=NOW)

    assert alert_latch.claimed is True


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
        assert await try_claim_scheduler_overdue_alert(
            session,
            alerted_at=NOW + timedelta(minutes=18),
            freeze_started_at=NOW,
            next_alert_at=NOW + timedelta(hours=1),
        )
        await session.commit()

    async def escalate() -> bool:
        async with AsyncSession(bind=db_engine, expire_on_commit=False) as session:
            won = await try_claim_scheduler_overdue_alert(
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


@pytest.mark.requires_db
async def test_postgres_concurrent_self_heal_claim_enforces_attempt_cap(
    db_engine,
):
    freeze_started_at = NOW - timedelta(minutes=20)
    async with AsyncSession(bind=db_engine, expire_on_commit=False) as session:
        await release_scheduler_alert(
            session,
            alert_key="scheduler_self_heal",
        )
        await session.commit()

    async def claim(attempted_at: datetime) -> SchedulerSelfHealState:
        async with AsyncSession(bind=db_engine, expire_on_commit=False) as session:
            state = await try_claim_scheduler_self_heal(
                session,
                attempted_at=attempted_at,
                freeze_started_at=freeze_started_at,
                next_attempt_at=attempted_at + timedelta(minutes=10),
                max_attempts=2,
            )
            await session.commit()
            return state

    try:
        first_wave = await asyncio.gather(claim(NOW), claim(NOW))
        assert sorted(
            state.attempt if state.attempt is not None else 0
            for state in first_wave
        ) == [0, 1]

        second_wave = await asyncio.gather(
            claim(NOW + timedelta(minutes=10)),
            claim(NOW + timedelta(minutes=10)),
        )
        assert sorted(
            state.attempt if state.attempt is not None else 0
            for state in second_wave
        ) == [0, 2]

        exhausted = await claim(NOW + timedelta(minutes=20))
        assert exhausted.attempt is None
        assert exhausted.attempts == 2
        assert exhausted.exhausted is True
    finally:
        async with AsyncSession(bind=db_engine) as session:
            await release_scheduler_alert(
                session,
                alert_key="scheduler_self_heal",
            )
            await session.commit()


def test_scheduler_alert_latch_has_no_job_owner_and_names_its_timestamp():
    table = SchedulerAlertLatch.__table__

    assert set(table.columns.keys()) == {
        "alert_key",
        "alerted_at",
        "freeze_started_at",
        "next_alert_at",
        "attempt_count",
    }
    assert table.columns["alert_key"].type.length == 80
    assert table.columns["alerted_at"].nullable is False
    assert table.columns["freeze_started_at"].nullable is True
    assert table.columns["next_alert_at"].nullable is True
    assert table.columns["attempt_count"].nullable is False
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


async def test_advancing_heartbeat_does_not_reset_overdue_escalation_clock():
    deliveries = []
    check_time = NOW
    next_run_at = NOW - timedelta(minutes=16)

    async def candidates(_session, *, now):
        return (
            SchedulerOverdueCandidate(
                job_key="uwear_aws_health_scan",
                next_run_at=next_run_at,
            ),
        )

    async def liveness_checkpoint(_session):
        return check_time

    async def deliver_alert(**kwargs):
        deliveries.append(kwargs)

    monitor = _monitor(
        candidate_provider=candidates,
        liveness_checkpoint=liveness_checkpoint,
        deliver_alert=deliver_alert,
    )

    await monitor._check(object(), now=check_time)
    check_time = NOW + timedelta(hours=1)
    await monitor._check(object(), now=check_time)

    assert [
        delivery["presentation"].title for delivery in deliveries
    ] == ["Scheduler jobs overdue", "Scheduler jobs overdue"]


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
        assert freeze_started_at == NOW - timedelta(minutes=45)
        assert next_alert_at == NOW + timedelta(minutes=15)
        assert transaction_open is True
        return True

    async def deliver_alert(**_kwargs):
        assert transaction_open is False

    async def no_self_heal(**_kwargs):
        return SchedulerSelfHealState(
            attempt=None,
            attempts=0,
            exhausted=False,
        )

    monitor = SchedulerOverdueMonitor(
        candidate_provider=candidates,
        liveness_checkpoint=liveness_checkpoint,
        deliver_alert=deliver_alert,
        claim_self_heal=no_self_heal,
    )
    with patch(
        "brain.platform.db.repositories.unit_of_work.UnitOfWork",
        FakeUnitOfWork,
    ), patch(
        "brain.app.scheduler.overdue_alert_state.try_claim_scheduler_overdue_alert",
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
