"""Scheduler trigger registry and persistence over the shared failure guard."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import os
from typing import (
    Callable,
    Literal,
    Mapping,
    Protocol,
    Sequence,
    TypeAlias,
)

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.contracts.scheduler_outcomes import SchedulerSkipKind
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
    async_latch_failure_edges,
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
STANDING_FAILURE_TRIGGER_KIND = FailureGuardTriggerKind("standing_failure")
CONFIGURATION_TRIGGER_KIND = FailureGuardTriggerKind("configuration")


@dataclass(frozen=True)
class SchedulerFailureGuardLifecycleContext:
    """Scheduler-owned inputs available to trigger evaluation and transitions."""

    session: AsyncSession
    job: SchedulerJob
    now: datetime
    record: FailureRecord | None
    failure_kind: SchedulerSkipKind | None = None


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
class StandingFailuresTrigger:
    """Alert after failures continue without a successful run."""

    threshold: int
    kind: FailureGuardTriggerKind = field(
        default=STANDING_FAILURE_TRIGGER_KIND,
        init=False,
    )

    @classmethod
    def from_settings(cls) -> StandingFailuresTrigger:
        return cls(
            threshold=_positive_int_setting(
                "SCHEDULER_STANDING_FAILURE_ALERT_THRESHOLD",
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
        return FailureGuardTriggerResult(
            kind=self.kind,
            active=count >= self.threshold,
            public_details={
                "count": count,
                "threshold": self.threshold,
                "window_hours": None,
            },
            alert_title="Scheduler job standing failure",
            alert_summary=f"Failures without a successful run: {count}",
        )

    async def transition_state(
        self,
        context: SchedulerFailureGuardLifecycleContext,
        state: FailureGuardTriggerState,
        *,
        event: FailureGuardLifecycleEvent,
    ) -> FailureGuardTriggerState | None:
        del context
        if event == "success":
            return None
        return {"count": int(state.get("count", 0)) + 1}

    async def should_reset(
        self,
        context: SchedulerFailureGuardLifecycleContext,
        *,
        event: SchedulerFailureGuardResetEvent,
    ) -> bool:
        del context
        return event == "success"


@dataclass(frozen=True)
class ConfigurationFailureTrigger:
    """Alert immediately while one job is blocked by configuration."""

    kind: FailureGuardTriggerKind = field(
        default=CONFIGURATION_TRIGGER_KIND,
        init=False,
    )

    async def evaluate_with_state(
        self,
        context: SchedulerFailureGuardLifecycleContext,
        *,
        state: FailureGuardTriggerState,
    ) -> FailureGuardTriggerResult:
        del context
        active = bool(state.get("active", False))
        return FailureGuardTriggerResult(
            kind=self.kind,
            active=active,
            public_details={
                "count": 1 if active else 0,
                "threshold": 1,
                "window_hours": None,
            },
            alert_title="Scheduler job blocked by missing configuration",
            alert_summary=str(
                state.get("summary")
                or "Job is blocked by missing configuration"
            ),
        )

    async def transition_state(
        self,
        context: SchedulerFailureGuardLifecycleContext,
        state: FailureGuardTriggerState,
        *,
        event: FailureGuardLifecycleEvent,
    ) -> FailureGuardTriggerState | None:
        del state
        if event == "success":
            return None
        if context.failure_kind is not SchedulerSkipKind.CONFIGURATION:
            return None
        if context.record is None:
            raise ValueError("configuration failure requires a failure record")
        return {
            "active": True,
            "summary": context.record.error_text,
        }

    async def should_reset(
        self,
        context: SchedulerFailureGuardLifecycleContext,
        *,
        event: SchedulerFailureGuardResetEvent,
    ) -> bool:
        del context
        return event == "success"


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
        return (await self.evaluate_many((context,)))[context.job.id]

    async def evaluate_many(
        self,
        contexts: Sequence[SchedulerFailureGuardLifecycleContext],
    ) -> Mapping[int, FailureGuardTriggerResult]:
        """Evaluate one rolling-window count query for a job projection."""
        if not contexts:
            return {}
        session = contexts[0].session
        now = contexts[0].now
        if any(
            context.session is not session or context.now != now
            for context in contexts
        ):
            raise ValueError(
                "Bulk scheduler trigger contexts must share a session and time"
            )
        job_ids = [context.job.id for context in contexts]
        result = await session.execute(
            select(SchedulerRun.job_id, func.count())
            .where(
                SchedulerRun.job_id.in_(job_ids),
                SchedulerRun.status == "settled_failure",
                SchedulerRun.started_at
                > now - timedelta(hours=self.window_hours),
            )
            .group_by(SchedulerRun.job_id)
        )
        counts = {
            int(job_id): int(count)
            for job_id, count in result.all()
        }
        return {
            context.job.id: self._result(
                counts.get(context.job.id, 0)
            )
            for context in contexts
        }

    def _result(self, count: int) -> FailureGuardTriggerResult:
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
    """One stateless scheduler trigger with mandatory bulk projection."""

    async def evaluate_many(
        self,
        contexts: Sequence[SchedulerFailureGuardLifecycleContext],
    ) -> Mapping[int, FailureGuardTriggerResult]:
        """Evaluate this trigger for every projected job."""


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
    StandingFailuresTrigger.from_settings,
    RollingWindowFailuresTrigger.from_settings,
    ConfigurationFailureTrigger,
)


def scheduler_failure_guard_registry() -> SchedulerFailureGuardRegistry:
    """Load the configured registry of independently owned triggers."""
    return SchedulerFailureGuardRegistry(
        triggers=tuple(
            provide_trigger()
            for provide_trigger in _FAILURE_GUARD_TRIGGER_PROVIDERS
        )
    )


@dataclass
class SchedulerFailureGuardStore:
    """Persist scheduler trigger latches behind the shared store contract."""

    session: AsyncSession
    job_id: int
    _latches: dict[
        FailureGuardTriggerKind,
        SchedulerFailureGuardLatch,
    ] | None = field(default=None, init=False, repr=False)

    @staticmethod
    def _statement(job_ids: Sequence[int]):
        return select(SchedulerFailureGuardLatch).where(
            SchedulerFailureGuardLatch.job_id.in_(job_ids)
        )

    @staticmethod
    def _index_latches(
        latches: Sequence[SchedulerFailureGuardLatch],
    ) -> dict[FailureGuardTriggerKind, SchedulerFailureGuardLatch]:
        return {
            FailureGuardTriggerKind(latch.trigger_kind): latch
            for latch in latches
        }

    @classmethod
    async def preload_many(
        cls,
        session: AsyncSession,
        job_ids: Sequence[int],
    ) -> dict[int, SchedulerFailureGuardStore]:
        """Return per-job stores hydrated by one latch query."""
        stores = {
            job_id: cls(session=session, job_id=job_id)
            for job_id in job_ids
        }
        latches_by_job: dict[int, list[SchedulerFailureGuardLatch]] = {
            job_id: []
            for job_id in job_ids
        }
        result = await session.scalars(cls._statement(job_ids))
        for latch in result.all():
            latches_by_job[latch.job_id].append(latch)
        for job_id, store in stores.items():
            store._latches = store._index_latches(latches_by_job[job_id])
        return stores

    async def load_latches(
        self,
    ) -> dict[FailureGuardTriggerKind, SchedulerFailureGuardLatch]:
        if self._latches is None:
            result = await self.session.scalars(self._statement((self.job_id,)))
            self._latches = self._index_latches(result.all())
        return dict(self._latches)

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
        if self._latches is not None:
            self._latches[trigger_kind] = latch
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
        if self._latches is not None:
            self._latches.pop(trigger_kind, None)


class SchedulerFailureGuardStateStore(
    SqlAlchemyFailureGuardStateStore[SchedulerFailureGuardTriggerState]
):
    """Persist scheduler trigger state through singular or prehydrated reads."""

    @staticmethod
    def _statement(job_ids: Sequence[int]):
        return select(SchedulerFailureGuardTriggerState).where(
            SchedulerFailureGuardTriggerState.job_id.in_(job_ids)
        )

    @classmethod
    def for_job(
        cls,
        session: AsyncSession,
        job_id: int,
    ) -> SchedulerFailureGuardStateStore:
        return cls(
            session=session,
            statement=cls._statement((job_id,)),
            create_record=lambda trigger_kind, trigger_state: (
                SchedulerFailureGuardTriggerState(
                    job_id=job_id,
                    trigger_kind=trigger_kind,
                    trigger_state=trigger_state,
                )
            ),
        )

    @classmethod
    async def preload_many(
        cls,
        session: AsyncSession,
        job_ids: Sequence[int],
    ) -> dict[int, SchedulerFailureGuardStateStore]:
        """Return per-job stores hydrated by one trigger-state query."""
        stores = {
            job_id: cls.for_job(session, job_id)
            for job_id in job_ids
        }
        records_by_job: dict[
            int,
            list[SchedulerFailureGuardTriggerState],
        ] = {
            job_id: []
            for job_id in job_ids
        }
        result = await session.scalars(cls._statement(job_ids))
        for record in result.all():
            records_by_job[record.job_id].append(record)
        for job_id, store in stores.items():
            store.preload_records(records_by_job[job_id])
        return stores


async def async_read_scheduler_failure_guards(
    session: AsyncSession,
    jobs: Sequence[SchedulerJob],
    *,
    now: datetime | None = None,
    registry: SchedulerFailureGuardRegistry | None = None,
) -> dict[int, FailureGuardEvaluation]:
    """Project every job's guard from bulk-loaded state and latches."""
    jobs = tuple(jobs)
    if not jobs:
        return {}

    now = ensure_utc(now)
    registry = registry or scheduler_failure_guard_registry()
    job_ids = [job.id for job in jobs]
    contexts = tuple(
        SchedulerFailureGuardLifecycleContext(
            session=session,
            job=job,
            now=now,
            record=None,
        )
        for job in jobs
    )
    latch_stores = await SchedulerFailureGuardStore.preload_many(
        session,
        job_ids,
    )
    state_stores = await SchedulerFailureGuardStateStore.preload_many(
        session,
        job_ids,
    )

    results_by_job: dict[
        int,
        dict[FailureGuardTriggerKind, FailureGuardTriggerResult],
    ] = {
        job_id: {}
        for job_id in job_ids
    }
    for trigger in registry.triggers:
        if isinstance(trigger, FailureGuardStatefulTrigger):
            continue
        trigger_results = dict(await trigger.evaluate_many(contexts))
        missing_job_ids = set(job_ids).difference(trigger_results)
        if missing_job_ids:
            raise ValueError(
                f"Bulk scheduler trigger {trigger.kind} omitted jobs: "
                + ", ".join(
                    str(job_id)
                    for job_id in sorted(missing_job_ids)
                )
            )
        for job_id in job_ids:
            results_by_job[job_id][trigger.kind] = trigger_results[job_id]

    stateful_triggers = tuple(
        trigger
        for trigger in registry.triggers
        if isinstance(trigger, FailureGuardStatefulTrigger)
    )
    evaluations: dict[int, FailureGuardEvaluation] = {}
    for job, context in zip(jobs, contexts, strict=True):
        for result in await async_evaluate_failure_guard_triggers(
            triggers=stateful_triggers,
            context=context,
            store=state_stores[job.id],
        ):
            results_by_job[job.id][result.kind] = result
        ordered_results = tuple(
            results_by_job[job.id][trigger.kind]
            for trigger in registry.triggers
        )
        evaluations[job.id] = await async_evaluate_failure_edges(
            results=ordered_results,
            failure_signature=job.failure_signature,
            last_error=job.last_failure_error,
            now=now,
            store=latch_stores[job.id],
            new_edge_mode="ignore",
        )
    return evaluations


async def async_read_scheduler_failure_guard(
    session: AsyncSession,
    job: SchedulerJob,
    *,
    now: datetime | None = None,
    registry: SchedulerFailureGuardRegistry | None = None,
) -> FailureGuardEvaluation:
    """Evaluate one scheduler guard through the canonical bulk reader."""
    evaluations = await async_read_scheduler_failure_guards(
        session,
        (job,),
        now=now,
        registry=registry,
    )
    return evaluations[job.id]


async def async_record_scheduler_job_failure(
    session: AsyncSession,
    job: SchedulerJob,
    *,
    failure_identity: str,
    error_text: str,
    now: datetime | None = None,
    failure_kind: SchedulerSkipKind | None = None,
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
    state_store = SchedulerFailureGuardStateStore.for_job(
        session,
        locked_job.id,
    )
    context = SchedulerFailureGuardLifecycleContext(
        session=session,
        job=locked_job,
        now=now,
        record=record,
        failure_kind=failure_kind,
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
        new_edge_mode="detect",
    )
    await session.flush()
    return evaluation


async def async_latch_scheduler_failure_alerts(
    session: AsyncSession,
    job: SchedulerJob,
    evaluation: FailureGuardEvaluation,
    *,
    now: datetime | None = None,
) -> FailureGuardEvaluation:
    """Latch detected alert edges after their delivery succeeds."""
    latched = await async_latch_failure_edges(
        evaluation,
        alerted_at=ensure_utc(now),
        store=SchedulerFailureGuardStore(session=session, job_id=job.id),
    )
    await session.flush()
    return latched


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
    state_store = SchedulerFailureGuardStateStore.for_job(
        session,
        locked_job.id,
    )
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
