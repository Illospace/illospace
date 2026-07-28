"""Scheduler trigger registry and persistence over the shared failure guard."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.kernel.common.time import ensure_utc
from brain.platform.db.models.scheduler import (
    SchedulerFailureGuardLatch,
    SchedulerJob,
    SchedulerRun,
)
from brain.systems.failure_guard import (
    CONSECUTIVE_TRIGGER_KIND,
    ROLLING_WINDOW_TRIGGER_KIND,
    FailureAlertSubject,
    FailureGuardEdge,
    FailureGuardEvaluation,
    FailureGuardLatch,
    FailureGuardRegistry,
    FailureGuardResetEvent,
    FailureGuardSubject,
    FailureGuardTrigger,
    FailureGuardTriggerKind,
    FailureGuardTriggerResult,
    async_deliver_failure_alert,
    async_evaluate_failure_guard,
    async_record_failure,
    async_reset_failure_guard,
    failure_signature as scheduler_failure_signature,
    normalize_failure_identity as normalize_scheduler_failure_identity,
    positive_int_setting as _positive_int_setting,
    serialize_failure_guard,
)

SCHEDULER_FAILURE_ALERT_THRESHOLD_DEFAULT = 3
SCHEDULER_FAILURE_RATE_THRESHOLD_DEFAULT = 3
SCHEDULER_FAILURE_RATE_WINDOW_HOURS_DEFAULT = 24

@dataclass(frozen=True)
class ConsecutiveFailuresTrigger:
    """Alert after the configured number of same-signature failures."""

    threshold: int
    kind: FailureGuardTriggerKind = field(
        default=CONSECUTIVE_TRIGGER_KIND,
        init=False,
    )

    @classmethod
    def from_settings(cls) -> ConsecutiveFailuresTrigger:
        return cls(
            threshold=_positive_int_setting(
                "SCHEDULER_FAILURE_ALERT_THRESHOLD",
                SCHEDULER_FAILURE_ALERT_THRESHOLD_DEFAULT,
            ),
        )

    async def evaluate(
        self,
        session: AsyncSession,
        job: SchedulerJob,
        now: datetime,
    ) -> FailureGuardTriggerResult:
        del session, now
        count = int(job.consecutive_failure_count or 0)
        return FailureGuardTriggerResult(
            active=count >= self.threshold,
            public_details={
                "count": count,
                "threshold": self.threshold,
                "window_hours": None,
            },
            alert_title="Scheduler job repeated failure",
            alert_summary=f"Consecutive failures: {count}",
        )

    async def should_reset(
        self,
        session: AsyncSession,
        job: SchedulerJob,
        now: datetime,
        *,
        event: FailureGuardResetEvent,
    ) -> bool:
        del session, job, now, event
        return True


@dataclass(frozen=True)
class RollingWindowFailuresTrigger:
    """Alert when failures reach a threshold within a rolling time window."""

    threshold: int
    window_hours: int
    kind: FailureGuardTriggerKind = field(
        default=ROLLING_WINDOW_TRIGGER_KIND,
        init=False,
    )

    @classmethod
    def from_settings(cls) -> RollingWindowFailuresTrigger:
        return cls(
            threshold=_positive_int_setting(
                "SCHEDULER_FAILURE_RATE_THRESHOLD",
                SCHEDULER_FAILURE_RATE_THRESHOLD_DEFAULT,
            ),
            window_hours=_positive_int_setting(
                "SCHEDULER_FAILURE_RATE_WINDOW_HOURS",
                SCHEDULER_FAILURE_RATE_WINDOW_HOURS_DEFAULT,
            ),
        )

    async def evaluate(
        self,
        session: AsyncSession,
        job: SchedulerJob,
        now: datetime,
    ) -> FailureGuardTriggerResult:
        count = int(
            await session.scalar(
                select(func.count())
                .select_from(SchedulerRun)
                .where(
                    SchedulerRun.job_id == job.id,
                    SchedulerRun.status == "settled_failure",
                    SchedulerRun.started_at
                    > now - timedelta(hours=self.window_hours),
                )
            )
            or 0
        )
        return FailureGuardTriggerResult(
            active=count >= self.threshold,
            public_details={
                "count": count,
                "threshold": self.threshold,
                "window_hours": self.window_hours,
            },
            alert_title="Scheduler job intermittent failure",
            alert_summary=(
                f"{count} failures in the last {self.window_hours}h "
                "(intermittent)"
            ),
        )

    async def should_reset(
        self,
        session: AsyncSession,
        job: SchedulerJob,
        now: datetime,
        *,
        event: FailureGuardResetEvent,
    ) -> bool:
        if event != "success":
            return False
        return not (await self.evaluate(session, job, now)).active


_FAILURE_GUARD_TRIGGER_PROVIDERS: tuple[
    Callable[[], FailureGuardTrigger],
    ...,
] = (
    ConsecutiveFailuresTrigger.from_settings,
    RollingWindowFailuresTrigger.from_settings,
)


def scheduler_failure_guard_registry() -> FailureGuardRegistry:
    """Load the configured registry of independently owned triggers."""
    return FailureGuardRegistry(
        triggers=tuple(
            provide_trigger()
            for provide_trigger in _FAILURE_GUARD_TRIGGER_PROVIDERS
        )
    )


async def _async_failure_guard_latches(
    session: AsyncSession,
    job_id: int,
) -> dict[FailureGuardTriggerKind, SchedulerFailureGuardLatch]:
    result = await session.scalars(
        select(SchedulerFailureGuardLatch).where(
            SchedulerFailureGuardLatch.job_id == job_id
        )
    )
    return {
        FailureGuardTriggerKind(latch.trigger_kind): latch
        for latch in result.all()
    }


async def async_read_scheduler_failure_guard(
    session: AsyncSession,
    job: SchedulerJob,
    *,
    now: datetime | None = None,
    registry: FailureGuardRegistry | None = None,
) -> FailureGuardEvaluation:
    """Read every registered trigger through the canonical evaluation path."""
    now = ensure_utc(now)
    registry = registry or scheduler_failure_guard_registry()
    latches = await _async_failure_guard_latches(session, job.id)
    return await async_evaluate_failure_guard(
        session,
        job,
        now=now,
        registry=registry,
        latches=latches,
        persist_crossings=False,
    )


async def async_record_scheduler_job_failure(
    session: AsyncSession,
    job: SchedulerJob,
    *,
    failure_identity: str,
    error_text: str,
    now: datetime | None = None,
    registry: FailureGuardRegistry | None = None,
) -> FailureGuardEvaluation:
    """Persist failure state and evaluate every trigger under one job lock."""
    now = ensure_utc(now)
    registry = registry or scheduler_failure_guard_registry()
    locked_job = await session.scalar(
        select(SchedulerJob)
        .where(SchedulerJob.id == job.id)
        .with_for_update()
    )
    if locked_job is None:
        raise ValueError(f"Scheduler job {job.id} not found")

    latches = await _async_failure_guard_latches(session, locked_job.id)

    async def create_latch(
        trigger_kind: FailureGuardTriggerKind,
        alerted_at: datetime,
    ) -> SchedulerFailureGuardLatch:
        latch = SchedulerFailureGuardLatch(
            job_id=locked_job.id,
            trigger_kind=str(trigger_kind),
            alerted_at=alerted_at,
        )
        session.add(latch)
        return latch

    async def delete_latch(
        trigger_kind: FailureGuardTriggerKind,
        latch: FailureGuardLatch,
    ) -> None:
        del trigger_kind
        await session.delete(latch)

    return await async_record_failure(
        session,
        locked_job,
        failure_identity=failure_identity,
        error_text=error_text,
        now=now,
        registry=registry,
        latches=latches,
        create_latch=create_latch,
        delete_latch=delete_latch,
    )


async def async_reset_scheduler_job_failure_guard(
    session: AsyncSession,
    job: SchedulerJob,
    *,
    now: datetime | None = None,
    registry: FailureGuardRegistry | None = None,
) -> None:
    """Reset trigger latches after a scheduler job succeeds."""
    now = ensure_utc(now)
    registry = registry or scheduler_failure_guard_registry()
    locked_job = await session.scalar(
        select(SchedulerJob)
        .where(SchedulerJob.id == job.id)
        .with_for_update()
    )
    if locked_job is None:
        raise ValueError(f"Scheduler job {job.id} not found")

    latches = await _async_failure_guard_latches(session, locked_job.id)

    async def delete_latch(
        trigger_kind: FailureGuardTriggerKind,
        latch: FailureGuardLatch,
    ) -> None:
        del trigger_kind
        await session.delete(latch)

    await async_reset_failure_guard(
        session,
        locked_job,
        now=now,
        registry=registry,
        latches=latches,
        delete_latch=delete_latch,
    )
