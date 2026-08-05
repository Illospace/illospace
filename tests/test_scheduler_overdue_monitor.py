from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from brain.app.scheduler.daemon import async_scheduler_health_snapshot
from brain.app.scheduler.overdue_monitor import (
    OVERDUE_FREEZE_TRIGGER_KIND,
    SCHEDULER_OVERDUE_CHECK_INTERVAL_SECONDS,
    async_check_scheduler_overdue_jobs,
)
from brain.platform.db.models.scheduler import SchedulerLease, SchedulerRun
from tests.scheduler_test_support import (
    make_scheduler_job,
    make_scheduler_test_session,
)


NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
async def scheduler_session(async_sqlite_session_factory):
    return await make_scheduler_test_session(async_sqlite_session_factory)


class MemoryTriggerStateStore:
    def __init__(self) -> None:
        self.states = {}

    async def load_trigger_states(self):
        return dict(self.states)

    async def save_trigger_state(self, trigger_kind, state):
        self.states[trigger_kind] = dict(state)

    async def delete_trigger_state(self, trigger_kind):
        self.states.pop(trigger_kind, None)


def test_overdue_monitor_checks_well_inside_twenty_minute_window():
    assert SCHEDULER_OVERDUE_CHECK_INTERVAL_SECONDS <= 5 * 60


def _snapshot(*, lag_seconds: int) -> dict:
    due_at = NOW - timedelta(seconds=lag_seconds)
    return {
        "daemon": {"owner_mode": "scheduler", "service_ready": True},
        "lag": {
            "lag_seconds": lag_seconds,
            "oldest_due_at": due_at.isoformat(),
            "lagging_jobs": [
                {
                    "job_key": "uwear_aws_health_scan",
                    "family": "uwear_aws_health_scan",
                    "next_run_at": due_at.isoformat(),
                    "lag_seconds": lag_seconds,
                    "pause_reason": None,
                }
            ],
        },
        "jobs": [{"id": 7, "job_key": "uwear_aws_health_scan"}],
    }


async def test_job_overdue_by_more_than_fifteen_minutes_alerts_with_tick_time():
    state_store = MemoryTriggerStateStore()
    deliveries = []
    last_tick_at = NOW - timedelta(minutes=16)

    async def health_snapshot(_session, *, now):
        return _snapshot(lag_seconds=16 * 60)

    async def liveness_checkpoint(_session):
        return last_tick_at

    async def deliver_alert(**kwargs):
        deliveries.append(kwargs)

    result = await async_check_scheduler_overdue_jobs(
        object(),
        now=NOW,
        health_snapshot=health_snapshot,
        liveness_checkpoint=liveness_checkpoint,
        state_store=state_store,
        deliver_alert=deliver_alert,
    )

    assert result.alert_sent is True
    assert result.overdue_job_keys == ("uwear_aws_health_scan",)
    assert len(deliveries) == 1
    alert = deliveries[0]
    assert alert["subject"].identity == "uwear_aws_health_scan"
    assert "16m overdue" in alert["presentation"].summary
    assert last_tick_at.isoformat() in alert["presentation"].summary
    assert OVERDUE_FREEZE_TRIGGER_KIND in state_store.states


async def test_all_jobs_on_schedule_for_twenty_four_hours_produces_no_alert():
    state_store = MemoryTriggerStateStore()
    deliveries = []

    async def health_snapshot(_session, *, now):
        return _snapshot(lag_seconds=0)

    async def liveness_checkpoint(_session):
        return NOW

    async def deliver_alert(**kwargs):
        deliveries.append(kwargs)

    for hour in range(25):
        result = await async_check_scheduler_overdue_jobs(
            object(),
            now=NOW + timedelta(hours=hour),
            health_snapshot=health_snapshot,
            liveness_checkpoint=liveness_checkpoint,
            state_store=state_store,
            deliver_alert=deliver_alert,
        )
        assert result.alert_sent is False
        assert result.overdue_job_keys == ()
    assert deliveries == []


async def test_alert_fires_once_per_freeze_and_rearms_after_recovery():
    state_store = MemoryTriggerStateStore()
    deliveries = []
    lag_seconds = 60 * 60

    async def health_snapshot(_session, *, now):
        return _snapshot(lag_seconds=lag_seconds)

    async def liveness_checkpoint(_session):
        return NOW

    async def deliver_alert(**kwargs):
        deliveries.append(kwargs)

    async def check(at: datetime):
        return await async_check_scheduler_overdue_jobs(
            object(),
            now=at,
            health_snapshot=health_snapshot,
            liveness_checkpoint=liveness_checkpoint,
            state_store=state_store,
            deliver_alert=deliver_alert,
        )

    first = await check(NOW)
    repeated = await check(NOW + timedelta(minutes=1))
    lag_seconds = 0
    recovered = await check(NOW + timedelta(minutes=2))
    lag_seconds = 60 * 60
    later_freeze = await check(NOW + timedelta(hours=2))

    assert first.alert_sent is True
    assert repeated.alert_sent is False
    assert recovered.alert_sent is False
    assert later_freeze.alert_sent is True
    assert len(deliveries) == 2


async def test_multiple_overdue_jobs_produce_one_freeze_alert():
    state_store = MemoryTriggerStateStore()
    deliveries = []

    async def health_snapshot(_session, *, now):
        snapshot = _snapshot(lag_seconds=60 * 60)
        snapshot["lag"]["lagging_jobs"].append(
            {
                "job_key": "knowledge_index_sync",
                "family": "knowledge_index_sync",
                "next_run_at": (NOW - timedelta(minutes=45)).isoformat(),
                "lag_seconds": 45 * 60,
                "pause_reason": None,
            }
        )
        return snapshot

    async def liveness_checkpoint(_session):
        return NOW - timedelta(hours=1)

    async def deliver_alert(**kwargs):
        deliveries.append(kwargs)

    result = await async_check_scheduler_overdue_jobs(
        object(),
        now=NOW,
        health_snapshot=health_snapshot,
        liveness_checkpoint=liveness_checkpoint,
        state_store=state_store,
        deliver_alert=deliver_alert,
    )

    assert result.overdue_job_keys == (
        "uwear_aws_health_scan",
        "knowledge_index_sync",
    )
    assert len(deliveries) == 1
    assert "uwear_aws_health_scan" in deliveries[0]["presentation"].summary
    assert "knowledge_index_sync" in deliveries[0]["presentation"].summary


async def test_fresh_daemon_heartbeat_does_not_hide_stale_next_run_at():
    state_store = MemoryTriggerStateStore()
    deliveries = []

    async def health_snapshot(_session, *, now):
        snapshot = _snapshot(lag_seconds=60 * 60)
        snapshot["daemon"].update(
            process_alive=True,
            heartbeat_at=NOW.isoformat(),
        )
        return snapshot

    async def liveness_checkpoint(_session):
        return NOW

    async def deliver_alert(**kwargs):
        deliveries.append(kwargs)

    result = await async_check_scheduler_overdue_jobs(
        object(),
        now=NOW,
        health_snapshot=health_snapshot,
        liveness_checkpoint=liveness_checkpoint,
        state_store=state_store,
        deliver_alert=deliver_alert,
    )

    assert result.alert_sent is True
    assert result.last_tick_at == NOW
    assert len(deliveries) == 1


async def test_scheduler_snapshot_skips_job_with_active_run(scheduler_session):
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

    assert snapshot["lag"]["lagging_jobs"] == []
    assert snapshot["health"]["status"] == "healthy"


async def test_database_trigger_state_deduplicates_and_rearms(scheduler_session):
    job = make_scheduler_job(
        job_key="stalled_job",
        family="stalled_job",
        program_key="stalled_job",
        next_run_at=NOW - timedelta(hours=1),
    )
    scheduler_session.add(job)
    await scheduler_session.flush()
    deliveries = []

    async def deliver_alert(**kwargs):
        deliveries.append(kwargs)

    async def check(at: datetime):
        return await async_check_scheduler_overdue_jobs(
            scheduler_session,
            now=at,
            deliver_alert=deliver_alert,
        )

    assert (await check(NOW)).alert_sent is True
    assert (await check(NOW + timedelta(minutes=1))).alert_sent is False
    job.next_run_at = NOW + timedelta(minutes=5)
    await scheduler_session.flush()
    assert (await check(NOW + timedelta(minutes=2))).alert_sent is False
    job.next_run_at = NOW - timedelta(hours=1)
    await scheduler_session.flush()
    assert (await check(NOW + timedelta(hours=2))).alert_sent is True
    assert len(deliveries) == 2
