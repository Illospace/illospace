"""Scheduler trigger registry and persistence over the shared failure guard."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import os
from typing import Callable

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.kernel.common.time import ensure_utc
from brain.platform.db.models.scheduler import (
    SchedulerFailureGuardLatch,
    SchedulerJob,
    SchedulerRun,
)
from brain.systems.failure_guard.core import (
    CONSECUTIVE_TRIGGER_KIND,
    ROLLING_WINDOW_TRIGGER_KIND,
    FailureGuardEvaluation,
    FailureGuardLatch,
    FailureGuardRegistry,
    FailureGuardResetEvent,
    FailureGuardTrigger,
    FailureGuardTriggerKind,
    FailureGuardTriggerResult,
    FailureObservation,
    async_read_failure_guard,
    async_record_failure,
    async_reset_failure_guard,
    failure_signature as scheduler_failure_signature,
    normalize_failure_identity as normalize_scheduler_failure_identity,
    serialize_failure_guard,
)

SCHEDULER_FAILURE_ALERT_THRESHOLD_DEFAULT = 3
SCHEDULER_FAILURE_RATE_THRESHOLD_DEFAULT = 3
SCHEDULER_FAILURE_RATE_WINDOW_HOURS_DEFAULT = 24


def _positive_int_setting(name: str, default: int) -> int:
    try:
        configured = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        configured = default
    return max(1, configured)


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
        *,
        observation: FailureObservation | None = None,
    ) -> FailureGuardTriggerResult:
        del session, now, observation
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
        *,
        observation: FailureObservation | None = None,
    ) -> FailureGuardTriggerResult:
        del observation
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


@dataclass(frozen=True)
class SchedulerFailureGuardStore:
    """Persist scheduler trigger latches behind the shared store contract."""

    session: AsyncSession
    job_id: int

    async def load_latches(
        self,
    ) -> dict[FailureGuardTriggerKind, SchedulerFailureGuardLatch]:
        result = await self.session.scalars(
            select(SchedulerFailureGuardLatch).where(
                SchedulerFailureGuardLatch.job_id == self.job_id
            )
        )
        return {
            FailureGuardTriggerKind(latch.trigger_kind): latch
            for latch in result.all()
        }

    async def create_latch(
        self,
        trigger_kind: FailureGuardTriggerKind,
        alerted_at: datetime,
    ) -> FailureGuardLatch:
        latch = SchedulerFailureGuardLatch(
            job_id=self.job_id,
            trigger_kind=str(trigger_kind),
            alerted_at=alerted_at,
        )
        self.session.add(latch)
        return latch

    async def delete_latch(
        self,
        trigger_kind: FailureGuardTriggerKind,
    ) -> None:
        await self.session.execute(
            delete(SchedulerFailureGuardLatch).where(
                SchedulerFailureGuardLatch.job_id == self.job_id,
                SchedulerFailureGuardLatch.trigger_kind == str(trigger_kind),
            )
        )


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
    return await async_read_failure_guard(
        session,
        job,
        now=now,
        registry=registry,
        store=SchedulerFailureGuardStore(session=session, job_id=job.id),
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

    consecutive_threshold = next(
        (
            int(trigger.threshold)
            for trigger in registry.triggers
            if trigger.kind == CONSECUTIVE_TRIGGER_KIND
            and hasattr(trigger, "threshold")
        ),
        1,
    )
    observation = FailureObservation(
        classification=(
            str(failure_identity).partition("\n")[0].strip()
            or "unclassified_failure"
        ),
        signature_input=failure_identity,
        error_text=error_text,
        alert_threshold=consecutive_threshold,
        alert_class="configured_trigger_failure",
        operator_action="Open scheduler state and resolve the failing job.",
    )

    return await async_record_failure(
        session,
        locked_job,
        observation=observation,
        now=now,
        registry=registry,
        store=SchedulerFailureGuardStore(
            session=session,
            job_id=locked_job.id,
        ),
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

    await async_reset_failure_guard(
        session,
        locked_job,
        now=now,
        registry=registry,
        store=SchedulerFailureGuardStore(
            session=session,
            job_id=locked_job.id,
        ),
    )
