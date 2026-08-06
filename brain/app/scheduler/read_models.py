"""Focused read models for scheduler policy consumers."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.app.scheduler.runtime import (
    RUN_STATUS_CLAIMED,
    RUN_STATUS_EXECUTING,
    RUN_STATUS_RUNNING,
)
from brain.platform.db.models.scheduler import (
    OWNER_MODE_SCHEDULER,
    SchedulerJob,
    SchedulerLease,
    SchedulerRun,
)


@dataclass(frozen=True, slots=True)
class SchedulerOverdueCandidate:
    """One past-due scheduler job that is not actively running."""

    job_key: str
    next_run_at: datetime

    def lag_seconds_at(self, now: datetime) -> int:
        """Return how many whole seconds the job is past due."""
        return max(0, int((_as_utc(now) - self.next_run_at).total_seconds()))


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def async_scheduler_overdue_candidates(
    session: AsyncSession,
    *,
    now: datetime,
) -> tuple[SchedulerOverdueCandidate, ...]:
    """Select monitor candidates without changing canonical scheduler health."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    active_run = (
        select(SchedulerRun.id)
        .join(SchedulerLease, SchedulerRun.lease_id == SchedulerLease.id)
        .where(
            SchedulerRun.job_id == SchedulerJob.id,
            SchedulerRun.status.in_(
                (RUN_STATUS_CLAIMED, RUN_STATUS_RUNNING, RUN_STATUS_EXECUTING)
            ),
            SchedulerLease.released_at.is_(None),
            SchedulerLease.expires_at > now,
        )
        .exists()
    )
    rows = (
        await session.execute(
            select(SchedulerJob.job_key, SchedulerJob.next_run_at)
            .where(
                SchedulerJob.owner_mode == OWNER_MODE_SCHEDULER,
                SchedulerJob.enabled.is_(True),
                SchedulerJob.pause_reason.is_(None),
                SchedulerJob.next_run_at.is_not(None),
                SchedulerJob.next_run_at <= now,
                ~active_run,
            )
            .order_by(SchedulerJob.next_run_at, SchedulerJob.job_key)
        )
    ).all()
    return tuple(
        SchedulerOverdueCandidate(
            job_key=str(job_key),
            next_run_at=_as_utc(next_run_at),
        )
        for job_key, next_run_at in rows
        if next_run_at is not None
    )


__all__ = [
    "SchedulerOverdueCandidate",
    "async_scheduler_overdue_candidates",
]
