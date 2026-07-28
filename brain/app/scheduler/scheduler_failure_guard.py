"""Scheduler trigger registry and persistence over the shared failure guard."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import os
from typing import Callable, Literal, Protocol, TypeAlias

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.kernel.common.time import ensure_utc
from brain.platform.db.models.scheduler import (
    SchedulerFailureGuardLatch,
    SchedulerFailureGuardTriggerState,
    SchedulerJob,
    SchedulerRun,
)
from brain.systems.failure_guard.core import (
    FailureRecord,
    FailureGuardEvaluation,
    FailureGuardLifecycleEvent,
    FailureGuardLatch,
    FailureGuardStatefulTrigger,
    FailureGuardTrigger,
    FailureGuardTriggerKind,
    FailureGuardTriggerResult,
    FailureGuardTriggerState,
    async_evaluate_failure_guard_triggers,
    async_evaluate_failure_edges,
    async_transition_failure_guard_trigger_states,
    failure_signature as scheduler_failure_signature,
    normalize_failure_identity as normalize_scheduler_failure_identity,
    serialize_failure_guard,
)
from brain.systems.failure_guard.state_repository import (
    SqlAlchemyFailureGuardStateStore,
)

SCHEDULER_FAILURE_ALERT_THRESHOLD_DEFAULT = 3
SCHEDULER_FAILURE_RATE_THRESHOLD_DEFAULT = 3
SCHEDULER_FAILURE_RATE_WINDOW_HOURS_DEFAULT = 24

SchedulerFailureGuardResetEvent = Literal["signature_change", "success"]
CONSECUTIVE_TRIGGER_KIND = FailureGuardTriggerKind("consecutive")
ROLLING_WINDOW_TRIGGER_KIND = FailureGuardTriggerKind("rolling_window")


@dataclass(frozen=True)
class SchedulerFailureGuardLifecycleContext:
    """Scheduler-owned inputs available to trigger evaluation and transitions."""

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

    async def evaluate_with_state(
        self,
        context: SchedulerFailureGuardLifecycleContext,
        *,
        state: FailureGuardTriggerState,
    ) -> FailureGuardTriggerResult:
        del context
        count = int(state.get("count", 0))
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
        state: FailureGuardTriggerState,
        *,
        event: FailureGuardLifecycleEvent,
    ) -> FailureGuardTriggerState | None:
        if event == "success":
            return None
        count = (
            1
            if event == "new_failure"
            else int(state.get("count", 0)) + 1
        )
        return {"count": count}

    async def should_reset(
        self,
        context: SchedulerFailureGuardLifecycleContext,
        *,
        event: SchedulerFailureGuardResetEvent,
    ) -> bool:
        del context, event
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
        context: SchedulerFailureGuardLifecycleContext,
    ) -> FailureGuardTriggerResult:
        count = int(
            await context.session.scalar(
                select(func.count())
                .select_from(SchedulerRun)
                .where(
                    SchedulerRun.job_id == context.job.id,
                    SchedulerRun.status == "settled_failure",
                    SchedulerRun.started_at
                    > context.now - timedelta(hours=self.window_hours),
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
        context: SchedulerFailureGuardLifecycleContext,
        *,
        event: SchedulerFailureGuardResetEvent,
    ) -> bool:
        if event != "success":
            return False
        return not (await self.evaluate(context)).active


class SchedulerFailureGuardResettableTrigger(Protocol):
    """Scheduler-owned latch-reset behavior shared by all trigger kinds."""

    kind: FailureGuardTriggerKind

    async def should_reset(
        self,
        context: SchedulerFailureGuardLifecycleContext,
        *,
        event: SchedulerFailureGuardResetEvent,
    ) -> bool:
        """Return whether this trigger's latch should reset."""


class SchedulerFailureGuardTrigger(
    SchedulerFailureGuardResettableTrigger,
    FailureGuardTrigger[SchedulerFailureGuardLifecycleContext],
    Protocol,
):
    """One stateless scheduler failure trigger."""


class SchedulerFailureGuardStatefulTrigger(
    SchedulerFailureGuardResettableTrigger,
    FailureGuardStatefulTrigger[SchedulerFailureGuardLifecycleContext],
    Protocol,
):
    """One state-owning scheduler failure trigger."""


SchedulerRegisteredFailureGuardTrigger: TypeAlias = (
    SchedulerFailureGuardTrigger | SchedulerFailureGuardStatefulTrigger
)


@dataclass(frozen=True)
class SchedulerFailureGuardRegistry:
    """The scheduler-owned set of failure triggers."""

    triggers: tuple[SchedulerRegisteredFailureGuardTrigger, ...]

    def __post_init__(self) -> None:
        kinds = [str(trigger.kind) for trigger in self.triggers]
        if any(not kind for kind in kinds):
            raise ValueError(
                "Scheduler failure-guard trigger kinds must not be empty"
            )
        if len(kinds) != len(set(kinds)):
            raise ValueError("Scheduler failure-guard trigger kinds must be unique")


_FAILURE_GUARD_TRIGGER_PROVIDERS: tuple[
    Callable[[], SchedulerRegisteredFailureGuardTrigger],
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


def _scheduler_failure_guard_state_store(
    session: AsyncSession,
    job_id: int,
) -> SqlAlchemyFailureGuardStateStore[SchedulerFailureGuardTriggerState]:
    return SqlAlchemyFailureGuardStateStore(
        session=session,
        statement=select(SchedulerFailureGuardTriggerState).where(
            SchedulerFailureGuardTriggerState.job_id == job_id
        ),
        create_record=lambda trigger_kind, trigger_state: (
            SchedulerFailureGuardTriggerState(
                job_id=job_id,
                trigger_kind=trigger_kind,
                trigger_state=trigger_state,
            )
        ),
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
    latch_store = SchedulerFailureGuardStore(session=session, job_id=job.id)
    context = SchedulerFailureGuardLifecycleContext(
        session=session,
        job=job,
        now=now,
        record=None,
    )
    results = await async_evaluate_failure_guard_triggers(
        triggers=registry.triggers,
        context=context,
        store=_scheduler_failure_guard_state_store(session, job.id),
    )
    return await async_evaluate_failure_edges(
        results=results,
        failure_signature=job.failure_signature,
        last_error=job.last_failure_error,
        now=now,
        store=latch_store,
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
    latch_store = SchedulerFailureGuardStore(
        session=session,
        job_id=locked_job.id,
    )
    state_store = _scheduler_failure_guard_state_store(session, locked_job.id)
    context = SchedulerFailureGuardLifecycleContext(
        session=session,
        job=locked_job,
        now=now,
        record=record,
    )
    signature = scheduler_failure_signature(record.signature_input)
    signature_changed = locked_job.failure_signature != signature
    if signature_changed:
        locked_job.failure_signature = signature
    await async_transition_failure_guard_trigger_states(
        triggers=registry.triggers,
        context=context,
        event="new_failure" if signature_changed else "repeated_failure",
        store=state_store,
    )
    if signature_changed:
        latches = dict(await latch_store.load_latches())
        reset_latch = False
        for trigger in registry.triggers:
            if trigger.kind not in latches or not await trigger.should_reset(
                context,
                event="signature_change",
            ):
                continue
            await latch_store.delete_latch(trigger.kind)
            reset_latch = True
        if reset_latch:
            await session.flush()

    locked_job.last_failure_error = record.error_text
    results = await async_evaluate_failure_guard_triggers(
        triggers=registry.triggers,
        context=context,
        store=state_store,
    )
    evaluation = await async_evaluate_failure_edges(
        results=results,
        failure_signature=locked_job.failure_signature,
        last_error=locked_job.last_failure_error,
        now=now,
        store=latch_store,
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

    latch_store = SchedulerFailureGuardStore(
        session=session,
        job_id=locked_job.id,
    )
    state_store = _scheduler_failure_guard_state_store(session, locked_job.id)
    context = SchedulerFailureGuardLifecycleContext(
        session=session,
        job=locked_job,
        now=now,
        record=None,
    )
    latches = dict(await latch_store.load_latches())
    for trigger in registry.triggers:
        if trigger.kind not in latches:
            continue
        if await trigger.should_reset(
            context,
            event="success",
        ):
            await latch_store.delete_latch(trigger.kind)

    await async_transition_failure_guard_trigger_states(
        triggers=registry.triggers,
        context=context,
        event="success",
        store=state_store,
    )
    locked_job.failure_signature = None
    locked_job.last_failure_error = None
    await session.flush()
