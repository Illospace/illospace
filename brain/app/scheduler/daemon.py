"""Scheduler daemon and operational health helpers."""
from __future__ import annotations

import asyncio
import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from brain.platform.db.models.scheduler import (
    OWNER_MODE_SCHEDULER,
    SchedulerJob,
    SchedulerLease,
    SchedulerRun,
)
from brain.app.scheduler.catalog import (
    DEFAULT_SCHEDULER_TIMEZONE,
    async_list_scheduler_jobs,
    async_list_scheduler_runs,
    async_sync_scheduler_catalog,
    normalize_owner_mode,
)
from brain.app.scheduler.executor import async_drain_scheduler
from brain.app.scheduler.cold_start import record_scheduler_liveness_checkpoint
from brain.app.scheduler.runtime import (
    RUN_STATUS_SHELVED,
    async_reclaim_expired_leases,
    normalize_run_status,
)

logger = logging.getLogger(__name__)
SCHEDULER_HEARTBEAT_INTERVAL = timedelta(seconds=15)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class _SchedulerLivenessHeartbeat:
    """Refresh liveness on an independent session while a tick is in flight."""

    def __init__(self, session: AsyncSession) -> None:
        self._session_factory = (
            async_sessionmaker(
                bind=session.bind,
                class_=AsyncSession,
                expire_on_commit=False,
            )
            if session.bind is not None
            else None
        )
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> _SchedulerLivenessHeartbeat:
        if self._session_factory is not None:
            self._task = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(SCHEDULER_HEARTBEAT_INTERVAL.total_seconds())
            if self._session_factory is None:
                return
            try:
                async with self._session_factory() as heartbeat_session:
                    await record_scheduler_liveness_checkpoint(heartbeat_session)
            except Exception:  # noqa: BLE001 - liveness retries on the next cadence
                logger.exception("Scheduler liveness heartbeat failed safely")


def _as_utc(dt_text: str | None) -> datetime | None:
    if not dt_text:
        return None
    parsed = datetime.fromisoformat(dt_text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _job_scope(jobs: list[dict[str, Any]], owner_mode: str) -> list[dict[str, Any]]:
    return [job for job in jobs if job.get("owner_mode") == owner_mode]


async def _async_count_run_statuses(session: AsyncSession, owner_mode: str) -> dict[str, int]:
    result = await session.execute(
        select(SchedulerRun.status, func.count())
        .join(SchedulerJob, SchedulerRun.job_id == SchedulerJob.id)
        .where(SchedulerJob.owner_mode == owner_mode)
        .group_by(SchedulerRun.status)
    )
    counts: dict[str, int] = {}
    for status, count in result.all():
        normalized = normalize_run_status(str(status))
        counts[normalized] = counts.get(normalized, 0) + int(count)
    return counts


async def _async_count_leases(
    session: AsyncSession,
    owner_mode: str,
    now: datetime,
) -> tuple[int, int]:
    active = await session.scalar(
        select(func.count())
        .select_from(SchedulerLease)
        .join(SchedulerRun, SchedulerLease.run_id == SchedulerRun.id)
        .join(SchedulerJob, SchedulerRun.job_id == SchedulerJob.id)
        .where(
            SchedulerJob.owner_mode == owner_mode,
            SchedulerLease.released_at.is_(None),
        )
    ) or 0
    expired = await session.scalar(
        select(func.count())
        .select_from(SchedulerLease)
        .join(SchedulerRun, SchedulerLease.run_id == SchedulerRun.id)
        .join(SchedulerJob, SchedulerRun.job_id == SchedulerJob.id)
        .where(
            SchedulerJob.owner_mode == owner_mode,
            SchedulerLease.released_at.is_(None),
            SchedulerLease.expires_at <= now,
        )
    ) or 0
    return int(active), int(expired)


def _scheduler_health_payload(
    *,
    owner_mode: str,
    now: datetime,
    jobs: list[dict[str, Any]],
    runs: list[dict[str, Any]],
    run_statuses: dict[str, int],
    active_leases: int,
    expired_leases: int,
) -> dict[str, Any]:
    scoped_jobs = _job_scope(jobs, owner_mode)
    alerts = []
    for job in scoped_jobs:
        latched_triggers = [
            trigger
            for trigger in job["failure_guard"]["triggers"]
            if trigger["alerted_at"] is not None
        ]
        if not latched_triggers:
            continue
        alerts.append(
            {
                "type": "scheduler_job_failure_guard",
                "job_key": job["job_key"],
                "failure_signature": job["failure_guard"]["failure_signature"],
                "last_error": job["failure_guard"]["last_error"],
                "triggers": latched_triggers,
            }
        )
    paused_jobs = [
        {
            "job_key": job["job_key"],
            "family": job["family"],
            "enabled": job["enabled"],
            "pause_reason": job["pause_reason"],
            "next_run_at": job["next_run_at"],
        }
        for job in scoped_jobs
        if not job["enabled"] or job["pause_reason"]
    ]
    lagging_jobs = []
    due_times: list[datetime] = []
    for job in scoped_jobs:
        next_run_at = _as_utc(job["next_run_at"])
        if not job["enabled"] or job["pause_reason"] or next_run_at is None or next_run_at > now:
            continue
        due_times.append(next_run_at)
        lagging_jobs.append(
            {
                "job_key": job["job_key"],
                "family": job["family"],
                "next_run_at": job["next_run_at"],
                "lag_seconds": int((now - next_run_at).total_seconds()),
                "pause_reason": job["pause_reason"],
            }
        )

    oldest_due_at = None
    lag_seconds = 0
    if due_times:
        oldest_due = min(due_times)
        if oldest_due is not None:
            oldest_due_at = oldest_due.isoformat()
            lag_seconds = int((now - oldest_due).total_seconds())

    all_scoped_paused = bool(scoped_jobs) and all(
        (not job["enabled"]) or bool(job["pause_reason"]) for job in scoped_jobs
    )
    health_reasons: list[str] = []
    if not scoped_jobs:
        health_status = "idle"
        health_reasons.append(f"no {owner_mode} jobs registered")
    elif all_scoped_paused:
        health_status = "paused"
        health_reasons.append(f"all {owner_mode} jobs are paused")
    else:
        health_status = "healthy"
        if lagging_jobs:
            health_status = "degraded"
            health_reasons.append(f"{len(lagging_jobs)} due {owner_mode} job(s) waiting to run")
            if oldest_due_at:
                health_reasons.append(f"oldest lagging run was due at {oldest_due_at}")
        if expired_leases:
            health_status = "degraded"
            health_reasons.append(f"{expired_leases} expired lease(s) need reclaim")
        if run_statuses.get("retryable", 0) or run_statuses.get(RUN_STATUS_SHELVED, 0):
            health_status = "degraded"
            if run_statuses.get("retryable", 0):
                health_reasons.append(f"{run_statuses['retryable']} retryable run(s)")
            if run_statuses.get(RUN_STATUS_SHELVED, 0):
                health_reasons.append(f"{run_statuses[RUN_STATUS_SHELVED]} shelved run(s)")
        if alerts:
            health_status = "degraded"
            health_reasons.append(
                f"{len(alerts)} scheduler job failure guard alert(s)"
            )

    job_owner_counts = Counter(job["owner_mode"] for job in jobs)
    jobs_enabled = sum(1 for job in scoped_jobs if job["enabled"])

    return {
        "now": now.isoformat(),
        "daemon": {
            "owner_mode": owner_mode,
            "service_ready": owner_mode == OWNER_MODE_SCHEDULER,
        },
        "summary": {
            "jobs_total": len(jobs),
            "jobs_in_scope": len(scoped_jobs),
            "jobs_enabled": jobs_enabled,
            "jobs_paused": len(paused_jobs),
            "jobs_by_owner_mode": dict(job_owner_counts),
            "runs_by_status": run_statuses,
            "active_leases": active_leases,
            "expired_leases": expired_leases,
            "lagging_jobs": len(lagging_jobs),
            "lag_seconds": lag_seconds,
            "active_alerts": len(alerts),
        },
        "health": {
            "status": health_status,
            "reasons": health_reasons,
        },
        "pause": {
            "paused_job_keys": [job["job_key"] for job in paused_jobs],
            "paused_jobs": paused_jobs,
            "global_pause": bool(scoped_jobs) and all_scoped_paused,
        },
        "lag": {
            "lag_seconds": lag_seconds,
            "oldest_due_at": oldest_due_at,
            "lagging_jobs": lagging_jobs,
        },
        "alerts": alerts,
        "jobs": jobs,
        "runs": runs,
    }


async def async_scheduler_health_snapshot(
    session: AsyncSession,
    *,
    owner_mode: str = OWNER_MODE_SCHEDULER,
    now: datetime | None = None,
    recent_run_limit: int = 20,
) -> dict[str, Any]:
    """Return scheduler health using native async database queries."""
    now = now or _utc_now()
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    owner_mode = normalize_owner_mode(owner_mode)
    jobs = await async_list_scheduler_jobs(session, now=now)
    runs = await async_list_scheduler_runs(session, limit=recent_run_limit)
    run_statuses = await _async_count_run_statuses(session, owner_mode)
    active_leases, expired_leases = await _async_count_leases(session, owner_mode, now)
    return _scheduler_health_payload(
        owner_mode=owner_mode,
        now=now,
        jobs=jobs,
        runs=runs,
        run_statuses=run_statuses,
        active_leases=active_leases,
        expired_leases=expired_leases,
    )


async def async_scheduler_daemon_startup(
    session: AsyncSession,
    *,
    owner_mode: str = OWNER_MODE_SCHEDULER,
    timezone_name: str = DEFAULT_SCHEDULER_TIMEZONE,
    now: datetime | None = None,
    cold_start_gap_threshold: timedelta | None = None,
) -> dict[str, Any]:
    """Synchronize the built-in catalog before the daemon starts draining work."""
    now = now or _utc_now()
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    owner_mode = normalize_owner_mode(owner_mode)
    try:
        from brain.app.scheduler.cold_start import (
            DEFAULT_COLD_START_GAP_THRESHOLD,
            reconcile_cold_start_gap,
        )

        cold_start = await reconcile_cold_start_gap(
            session,
            now=now,
            threshold=(
                cold_start_gap_threshold
                if cold_start_gap_threshold is not None
                else DEFAULT_COLD_START_GAP_THRESHOLD
            ),
        )
    except Exception as exc:  # noqa: BLE001 - startup reconciliation is fail-open
        logger.exception("Scheduler cold-start reconciliation failed safely")
        await session.rollback()
        cold_start = {
            "triggered": False,
            "reason": "reconciliation_failed",
            "error": str(exc),
        }
    catalog = await async_sync_scheduler_catalog(
        session,
        owner_mode=owner_mode,
        timezone_name=timezone_name,
        now=now,
    )
    snapshot = await async_scheduler_health_snapshot(
        session,
        owner_mode=owner_mode,
        now=now,
    )
    logger.info(
        "Scheduler catalog synchronized at daemon startup: upserted=%s retired=%s "
        "jobs_total=%s jobs_enabled=%s",
        catalog["upserted"],
        catalog["retired"],
        snapshot["summary"]["jobs_total"],
        snapshot["summary"]["jobs_enabled"],
    )
    await session.flush()
    return {
        "ok": True,
        "owner_mode": owner_mode,
        "cold_start": cold_start,
        "catalog": catalog,
        "snapshot": snapshot,
    }


async def async_scheduler_daemon_tick(
    session: AsyncSession,
    *,
    owner_mode: str = OWNER_MODE_SCHEDULER,
    job_key: str | None = None,
    max_runs: int = 10,
    resume: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run one scheduler tick and return only events produced by this tick."""
    now = now or _utc_now()
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    owner_mode = normalize_owner_mode(owner_mode)
    await record_scheduler_liveness_checkpoint(session, now=now)
    async with _SchedulerLivenessHeartbeat(session):
        reclaimed = await async_reclaim_expired_leases(session, now=now)
        drain = await async_drain_scheduler(
            session,
            owner_mode=owner_mode,
            job_key=job_key,
            max_runs=max_runs,
            resume=resume,
            now=now,
        )
    await session.flush()
    return {
        "ok": True,
        "owner_mode": owner_mode,
        "reclaimed": len(reclaimed),
        "reclaimed_run_ids": [run.id for run in reclaimed],
        "drain": drain,
    }
