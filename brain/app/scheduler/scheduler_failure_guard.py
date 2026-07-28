"""Scheduler trigger registry and persistence over the shared failure guard."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import os
from typing import Any, Callable, Literal, Mapping, Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.kernel.common.time import ensure_utc
from brain.platform.db.models.scheduler import (
    SchedulerFailureGuardLatch,
    SchedulerJob,
    SchedulerRun,
)
from brain.systems.failure_guard.core import (
    FailureRecord,
    FailureGuardEvaluation,
    FailureGuardLifecycleEvent,
    FailureGuardLatch,
    FailureGuardTriggerKind,
    FailureGuardTriggerResult,
    async_evaluate_failure_edges,
    async_transition_failure_guard_trigger_states,
    failure_signature as scheduler_failure_signature,
    normalize_failure_identity as normalize_scheduler_failure_identity,
    serialize_failure_guard,
)

SCHEDULER_FAILURE_ALERT_THRESHOLD_DEFAULT = 3
SCHEDULER_FAILURE_RATE_THRESHOLD_DEFAULT = 3
SCHEDULER_FAILURE_RATE_WINDOW_HOURS_DEFAULT = 24

SchedulerFailureGuardResetEvent = Literal["signature_change", "success"]
CONSECUTIVE_TRIGGER_KIND = FailureGuardTriggerKind("consecutive")
ROLLING_WINDOW_TRIGGER_KIND = FailureGuardTriggerKind("rolling_window")


@dataclass(frozen=True)
class SchedulerFailureGuardLifecycleContext:
    """Scheduler-owned inputs available to trigger state transitions."""

    session: AsyncSession
    job: SchedulerJob
    now: datetime
    record: FailureRecord | None


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
    ) -> FailureGuardTriggerResult:
        del session, now
        count = int(job.consecutive_failure_count or 0)
        return self._result(count)

    async def evaluate_with_state(
        self,
        session: AsyncSession,
        job: SchedulerJob,
        now: datetime,
        *,
        state: Mapping[str, Any],
    ) -> FailureGuardTriggerResult:
        del session, now
        count = int(
            state.get("count", job.consecutive_failure_count or 0)
        )
        return self._result(count)

    def _result(self, count: int) -> FailureGuardTriggerResult:
        return FailureGuardTriggerResult(
            kind=self.kind,
            active=count >= self.threshold,
            public_details={
                "count": count,
                "threshold": self.threshold,
                "window_hours": None,
            },
            alert_title="Scheduler job repeated failure",
            alert_summary=f"Consecutive failures: {count}",
        )

    async def transition_state(
        self,
        context: SchedulerFailureGuardLifecycleContext,
        state: Mapping[str, Any],
        *,
        event: FailureGuardLifecycleEvent,
    ) -> Mapping[str, Any] | None:
        if event == "success":
            context.job.consecutive_failure_count = 0
            return None
        count = (
            1
            if event == "new_failure"
            else int(
                state.get(
                    "count",
                    context.job.consecutive_failure_count or 0,
                )
            )
            + 1
        )
        context.job.consecutive_failure_count = count
        return {"count": count}

    async def should_reset(
        self,
        session: AsyncSession,
        job: SchedulerJob,
        now: datetime,
        *,
        event: SchedulerFailureGuardResetEvent,
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
            kind=self.kind,
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
        event: SchedulerFailureGuardResetEvent,
    ) -> bool:
        if event != "success":
            return False
        return not (await self.evaluate(session, job, now)).active


class SchedulerFailureGuardTrigger(Protocol):
    """One scheduler-owned failure trigger."""

    kind: FailureGuardTriggerKind

    async def evaluate(
        self,
        session: AsyncSession,
        job: SchedulerJob,
        now: datetime,
    ) -> FailureGuardTriggerResult:
        """Evaluate scheduler state and construct public presentation."""

    async def should_reset(
        self,
        session: AsyncSession,
        job: SchedulerJob,
        now: datetime,
        *,
        event: SchedulerFailureGuardResetEvent,
    ) -> bool:
        """Return whether this trigger's latch should reset."""


@dataclass(frozen=True)
class SchedulerFailureGuardRegistry:
    """The scheduler-owned set of failure triggers."""

    triggers: tuple[SchedulerFailureGuardTrigger, ...]

    def __post_init__(self) -> None:
        kinds = [str(trigger.kind) for trigger in self.triggers]
        if any(not kind for kind in kinds):
            raise ValueError(
                "Scheduler failure-guard trigger kinds must not be empty"
            )
        if len(kinds) != len(set(kinds)):
            raise ValueError("Scheduler failure-guard trigger kinds must be unique")


_FAILURE_GUARD_TRIGGER_PROVIDERS: tuple[
    Callable[[], SchedulerFailureGuardTrigger],
    ...,
] = (
    ConsecutiveFailuresTrigger.from_settings,
    RollingWindowFailuresTrigger.from_settings,
)


def scheduler_failure_guard_registry() -> SchedulerFailureGuardRegistry:
    """Load the configured registry of independently owned triggers."""
    return SchedulerFailureGuardRegistry(
        triggers=tuple(
            provide_trigger()
            for provide_trigger in _FAILURE_GUARD_TRIGGER_PROVIDERS
        )
    )


async def _async_evaluate_scheduler_triggers(
    session: AsyncSession,
    job: SchedulerJob,
    now: datetime,
    registry: SchedulerFailureGuardRegistry,
    store: SchedulerFailureGuardStore,
) -> tuple[FailureGuardTriggerResult, ...]:
    states = dict(await store.load_trigger_states())
    results = []
    for trigger in registry.triggers:
        evaluate_with_state = getattr(trigger, "evaluate_with_state", None)
        if evaluate_with_state is None:
            results.append(await trigger.evaluate(session, job, now))
            continue
        results.append(
            await evaluate_with_state(
                session,
                job,
                now,
                state=dict(states.get(trigger.kind, {})),
            )
        )
    return tuple(results)


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
                SchedulerFailureGuardLatch.job_id == self.job_id,
                SchedulerFailureGuardLatch.alerted_at.is_not(None),
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
        latch = await self._load_record(trigger_kind)
        if latch is None:
            latch = SchedulerFailureGuardLatch(
                job_id=self.job_id,
                trigger_kind=str(trigger_kind),
                trigger_state={},
                alerted_at=alerted_at,
            )
            self.session.add(latch)
        else:
            latch.alerted_at = alerted_at
        return latch

    async def delete_latch(
        self,
        trigger_kind: FailureGuardTriggerKind,
    ) -> None:
        latch = await self._load_record(trigger_kind)
        if latch is None:
            return
        if latch.trigger_state:
            latch.alerted_at = None
        else:
            await self.session.delete(latch)

    async def load_trigger_states(
        self,
    ) -> dict[FailureGuardTriggerKind, Mapping[str, Any]]:
        result = await self.session.scalars(
            select(SchedulerFailureGuardLatch).where(
                SchedulerFailureGuardLatch.job_id == self.job_id
            )
        )
        return {
            FailureGuardTriggerKind(record.trigger_kind): dict(
                record.trigger_state or {}
            )
            for record in result.all()
            if record.trigger_state
        }

    async def save_trigger_state(
        self,
        trigger_kind: FailureGuardTriggerKind,
        state: Mapping[str, Any],
    ) -> None:
        record = await self._load_record(trigger_kind)
        if record is None:
            self.session.add(
                SchedulerFailureGuardLatch(
                    job_id=self.job_id,
                    trigger_kind=str(trigger_kind),
                    trigger_state=dict(state),
                    alerted_at=None,
                )
            )
            return
        record.trigger_state = dict(state)

    async def delete_trigger_state(
        self,
        trigger_kind: FailureGuardTriggerKind,
    ) -> None:
        record = await self._load_record(trigger_kind)
        if record is None:
            return
        if record.alerted_at is None:
            await self.session.delete(record)
        else:
            record.trigger_state = {}

    async def _load_record(
        self,
        trigger_kind: FailureGuardTriggerKind,
    ) -> SchedulerFailureGuardLatch | None:
        return await self.session.scalar(
            select(SchedulerFailureGuardLatch).where(
                SchedulerFailureGuardLatch.job_id == self.job_id,
                SchedulerFailureGuardLatch.trigger_kind == str(trigger_kind),
            )
        )


async def async_read_scheduler_failure_guard(
    session: AsyncSession,
    job: SchedulerJob,
    *,
    now: datetime | None = None,
    registry: SchedulerFailureGuardRegistry | None = None,
) -> FailureGuardEvaluation:
    """Evaluate scheduler triggers and read their durable latch edges."""
    now = ensure_utc(now)
    registry = registry or scheduler_failure_guard_registry()
    store = SchedulerFailureGuardStore(session=session, job_id=job.id)
    results = await _async_evaluate_scheduler_triggers(
        session,
        job,
        now,
        registry,
        store,
    )
    return await async_evaluate_failure_edges(
        results=results,
        failure_signature=job.failure_signature,
        last_error=job.last_failure_error,
        now=now,
        store=store,
        latch_new_edges=False,
    )


async def async_record_scheduler_job_failure(
    session: AsyncSession,
    job: SchedulerJob,
    *,
    failure_identity: str,
    error_text: str,
    now: datetime | None = None,
    registry: SchedulerFailureGuardRegistry | None = None,
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

    record = FailureRecord(
        signature_input=failure_identity,
        error_text=error_text,
    )
    store = SchedulerFailureGuardStore(
        session=session,
        job_id=locked_job.id,
    )
    signature = scheduler_failure_signature(record.signature_input)
    signature_changed = locked_job.failure_signature != signature
    if signature_changed:
        locked_job.failure_signature = signature
    await async_transition_failure_guard_trigger_states(
        triggers=registry.triggers,
        context=SchedulerFailureGuardLifecycleContext(
            session=session,
            job=locked_job,
            now=now,
            record=record,
        ),
        event="new_failure" if signature_changed else "repeated_failure",
        store=store,
    )
    if signature_changed:
        latches = dict(await store.load_latches())
        reset_latch = False
        for trigger in registry.triggers:
            if trigger.kind not in latches or not await trigger.should_reset(
                session,
                locked_job,
                now,
                event="signature_change",
            ):
                continue
            await store.delete_latch(trigger.kind)
            reset_latch = True
        if reset_latch:
            await session.flush()

    locked_job.last_failure_error = record.error_text
    results = await _async_evaluate_scheduler_triggers(
        session,
        locked_job,
        now,
        registry,
        store,
    )
    evaluation = await async_evaluate_failure_edges(
        results=results,
        failure_signature=locked_job.failure_signature,
        last_error=locked_job.last_failure_error,
        now=now,
        store=store,
        latch_new_edges=True,
    )
    await session.flush()
    return evaluation


async def async_reset_scheduler_job_failure_guard(
    session: AsyncSession,
    job: SchedulerJob,
    *,
    now: datetime | None = None,
    registry: SchedulerFailureGuardRegistry | None = None,
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

    store = SchedulerFailureGuardStore(
        session=session,
        job_id=locked_job.id,
    )
    latches = dict(await store.load_latches())
    for trigger in registry.triggers:
        if trigger.kind not in latches:
            continue
        if await trigger.should_reset(
            session,
            locked_job,
            now,
            event="success",
        ):
            await store.delete_latch(trigger.kind)

    await async_transition_failure_guard_trigger_states(
        triggers=registry.triggers,
        context=SchedulerFailureGuardLifecycleContext(
            session=session,
            job=locked_job,
            now=now,
            record=None,
        ),
        event="success",
        store=store,
    )
    locked_job.failure_signature = None
    locked_job.last_failure_error = None
    await session.flush()
