"""Focused read models for scheduler policy consumers."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import and_, case, func, or_, select
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


@dataclass(frozen=True, slots=True)
class SchedulerFailureCandidate:
    """One enabled job with a standing all-failure or failure-streak state."""

    job_key: str
    next_run_at: datetime | None
    failure_count: int
    lifetime_success_count: int
    failure_signature: str | None
    last_failure_error: str | None


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


async def async_scheduler_failure_candidates(
    session: AsyncSession,
    *,
    failure_streak_threshold: int,
) -> tuple[SchedulerFailureCandidate, ...]:
    """Select standing failure states independently of scheduler timeliness."""
    if failure_streak_threshold < 1:
        raise ValueError("failure_streak_threshold must be positive")

    terminal_at = func.coalesce(
        SchedulerRun.finished_at,
        SchedulerRun.started_at,
        SchedulerRun.scheduled_for,
    )
    run_history = (
        select(
            SchedulerRun.job_id.label("job_id"),
            SchedulerRun.status.label("status"),
            terminal_at.label("terminal_at"),
            func.max(
                case(
                    (SchedulerRun.status == "settled_success", terminal_at),
                    else_=None,
                )
            )
            .over(partition_by=SchedulerRun.job_id)
            .label("last_success_at"),
        )
        .where(
            SchedulerRun.status.in_(
                ("settled_failure", "settled_success")
            )
        )
        .subquery()
    )
    run_health = (
        select(
            run_history.c.job_id,
            func.sum(
                case(
                    (run_history.c.status == "settled_success", 1),
                    else_=0,
                )
            ).label("lifetime_success_count"),
            func.sum(
                case(
                    (
                        and_(
                            run_history.c.status == "settled_failure",
                            or_(
                                run_history.c.last_success_at.is_(None),
                                run_history.c.terminal_at
                                > run_history.c.last_success_at,
                            ),
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("failure_count"),
        )
        .group_by(run_history.c.job_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(
                SchedulerJob.job_key,
                SchedulerJob.next_run_at,
                run_health.c.failure_count,
                run_health.c.lifetime_success_count,
                SchedulerJob.failure_signature,
                SchedulerJob.last_failure_error,
            )
            .join(run_health, run_health.c.job_id == SchedulerJob.id)
            .where(
                SchedulerJob.owner_mode == OWNER_MODE_SCHEDULER,
                SchedulerJob.enabled.is_(True),
                SchedulerJob.pause_reason.is_(None),
                or_(
                    and_(
                        run_health.c.lifetime_success_count == 0,
                        run_health.c.failure_count > 0,
                    ),
                    run_health.c.failure_count >= failure_streak_threshold,
                ),
            )
            .order_by(SchedulerJob.job_key)
        )
    ).all()
    return tuple(
        SchedulerFailureCandidate(
            job_key=str(job_key),
            next_run_at=(
                _as_utc(next_run_at) if next_run_at is not None else None
            ),
            failure_count=int(failure_count),
            lifetime_success_count=int(lifetime_success_count),
            failure_signature=(
                str(failure_signature) if failure_signature else None
            ),
            last_failure_error=(
                str(last_failure_error) if last_failure_error else None
            ),
        )
        for (
            job_key,
            next_run_at,
            failure_count,
            lifetime_success_count,
            failure_signature,
            last_failure_error,
        ) in rows
    )


__all__ = [
    "SchedulerFailureCandidate",
    "SchedulerOverdueCandidate",
    "async_scheduler_failure_candidates",
    "async_scheduler_overdue_candidates",
]
