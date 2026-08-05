"""Alert when scheduler-owned jobs stop advancing."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import os
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.app.scheduler.cold_start import scheduler_liveness_checkpoint
from brain.app.scheduler.daemon import async_scheduler_health_snapshot
from brain.app.scheduler.scheduler_failure_guard import (
    SchedulerFailureGuardStateStore,
)
from brain.platform.db.models.scheduler import SchedulerJob
from brain.systems.cortex.thread_links import public_app_base_url
from brain.systems.failure_guard.core import (
    FailureGuardStateStore,
    FailureGuardTriggerKind,
)
from brain.systems.failure_guard.slack_delivery import (
    FailureAlertPresentation,
    FailureAlertSubject,
    SlackFailureAlertPolicy,
    async_deliver_failure_alert,
)
from brain.systems.slack.client import slack_web_client_from_runtime


SCHEDULER_OVERDUE_AFTER = timedelta(minutes=15)
SCHEDULER_OVERDUE_CHECK_INTERVAL_SECONDS = 60.0
OVERDUE_FREEZE_TRIGGER_KIND = FailureGuardTriggerKind("scheduler_overdue_freeze")
UnitOfWork = None
logger = logging.getLogger(__name__)

HealthSnapshotProvider = Callable[..., Awaitable[dict[str, Any]]]
LivenessCheckpointProvider = Callable[[Any], Awaitable[datetime | None]]
AlertDelivery = Callable[..., Awaitable[None]]


@dataclass(frozen=True, slots=True)
class SchedulerOverdueCheck:
    """Observable outcome of one independent overdue check."""

    overdue_job_keys: tuple[str, ...]
    alert_sent: bool
    last_tick_at: datetime | None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _unit_of_work_factory():
    global UnitOfWork
    if UnitOfWork is None:
        from brain.platform.db.repositories.unit_of_work import UnitOfWork as _UnitOfWork

        UnitOfWork = _UnitOfWork
    return UnitOfWork


def _overdue_jobs(snapshot: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    threshold_seconds = int(SCHEDULER_OVERDUE_AFTER.total_seconds())
    return tuple(
        job
        for job in snapshot.get("lag", {}).get("lagging_jobs", ())
        if int(job.get("lag_seconds", 0)) > threshold_seconds
    )


def _alert_summary(
    jobs: tuple[dict[str, Any], ...],
    *,
    last_tick_at: datetime | None,
) -> str:
    job_lines = [
        f"- {job['job_key']}: {int(job['lag_seconds']) // 60}m overdue "
        f"(due {job['next_run_at']})"
        for job in jobs
    ]
    tick_text = last_tick_at.isoformat() if last_tick_at else "unknown"
    return "\n".join(("Overdue jobs:", *job_lines, f"Daemon last tick: {tick_text}"))


async def async_check_scheduler_overdue_jobs(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    health_snapshot: HealthSnapshotProvider = async_scheduler_health_snapshot,
    liveness_checkpoint: LivenessCheckpointProvider = scheduler_liveness_checkpoint,
    state_store: FailureGuardStateStore | None = None,
    deliver_alert: AlertDelivery = async_deliver_failure_alert,
) -> SchedulerOverdueCheck:
    """Check scheduler progress from a process outside the scheduler daemon."""
    now = now or _utc_now()
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    snapshot = await health_snapshot(session, now=now)
    overdue_jobs = _overdue_jobs(snapshot)
    last_tick_at = await liveness_checkpoint(session)
    if state_store is None:
        job_ids = [
            int(job["id"])
            for job in snapshot.get("jobs", ())
            if job.get("id") is not None
        ]
        if not job_ids:
            return SchedulerOverdueCheck(
                overdue_job_keys=tuple(
                    str(job["job_key"]) for job in overdue_jobs
                ),
                alert_sent=False,
                last_tick_at=last_tick_at,
            )
        anchor_job_id = min(job_ids)
        anchor_job = await session.scalar(
            select(SchedulerJob)
            .where(SchedulerJob.id == anchor_job_id)
            .with_for_update()
        )
        if anchor_job is None:
            return SchedulerOverdueCheck(
                overdue_job_keys=tuple(
                    str(job["job_key"]) for job in overdue_jobs
                ),
                alert_sent=False,
                last_tick_at=last_tick_at,
            )
        state_store = SchedulerFailureGuardStateStore.for_job(
            session,
            anchor_job.id,
        )

    states = await state_store.load_trigger_states()
    already_alerted = OVERDUE_FREEZE_TRIGGER_KIND in states
    overdue_job_keys = tuple(str(job["job_key"]) for job in overdue_jobs)
    if not overdue_jobs:
        if already_alerted:
            await state_store.delete_trigger_state(OVERDUE_FREEZE_TRIGGER_KIND)
        return SchedulerOverdueCheck(
            overdue_job_keys=(),
            alert_sent=False,
            last_tick_at=last_tick_at,
        )
    if already_alerted:
        return SchedulerOverdueCheck(
            overdue_job_keys=overdue_job_keys,
            alert_sent=False,
            last_tick_at=last_tick_at,
        )

    await deliver_alert(
        policy=SlackFailureAlertPolicy(
            provide_client=slack_web_client_from_runtime,
            requested_by="scheduler_overdue_alert",
            reason="Deliver an overdue scheduler job alert to the team.",
            channel=(
                os.getenv("ILLO_SCHEDULER_FAILURE_ALERT_CHANNEL", "").strip()
                or "#alerts"
            ),
            unknown_error_text="Scheduler jobs stopped advancing",
        ),
        subject=FailureAlertSubject(
            identity_label="Job key",
            identity=str(overdue_jobs[0]["job_key"]),
            url_label="Scheduler",
            url=f"{public_app_base_url()}/api/system/scheduler",
            link_label="open scheduler state",
        ),
        presentation=FailureAlertPresentation(
            title="Scheduler jobs overdue",
            summary=_alert_summary(overdue_jobs, last_tick_at=last_tick_at),
        ),
        error_text="Scheduler jobs stopped advancing past next_run_at.",
    )
    await state_store.save_trigger_state(
        OVERDUE_FREEZE_TRIGGER_KIND,
        {"alerted_at": now.isoformat()},
    )
    return SchedulerOverdueCheck(
        overdue_job_keys=overdue_job_keys,
        alert_sent=True,
        last_tick_at=last_tick_at,
    )


async def async_monitor_scheduler_overdue_jobs(
    *,
    now: datetime | None = None,
) -> SchedulerOverdueCheck:
    """Run one overdue check in its own worker-owned transaction."""
    async with _unit_of_work_factory()() as uow:
        return await async_check_scheduler_overdue_jobs(
            uow.session,
            now=now,
        )


async def async_scheduler_overdue_monitor_loop() -> None:
    """Check overdue scheduler state on the independent API event loop."""
    while True:
        await asyncio.sleep(SCHEDULER_OVERDUE_CHECK_INTERVAL_SECONDS)
        try:
            await async_monitor_scheduler_overdue_jobs()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the next monitor interval retries
            logger.exception("Scheduler overdue health check failed safely")


__all__ = [
    "OVERDUE_FREEZE_TRIGGER_KIND",
    "SCHEDULER_OVERDUE_CHECK_INTERVAL_SECONDS",
    "SCHEDULER_OVERDUE_AFTER",
    "SchedulerOverdueCheck",
    "async_check_scheduler_overdue_jobs",
    "async_monitor_scheduler_overdue_jobs",
    "async_scheduler_overdue_monitor_loop",
]
