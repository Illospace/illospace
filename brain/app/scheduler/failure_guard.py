"""Extensible scheduler failure-guard triggers and evaluation engine."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from hashlib import sha256
import os
import re
from typing import Any, Callable, Literal, Mapping, NewType, Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.kernel.common.time import ensure_utc
from brain.platform.db.models.scheduler import (
    SchedulerFailureGuardLatch,
    SchedulerJob,
    SchedulerRun,
)

SCHEDULER_FAILURE_ALERT_THRESHOLD_DEFAULT = 3
SCHEDULER_FAILURE_RATE_THRESHOLD_DEFAULT = 3
SCHEDULER_FAILURE_RATE_WINDOW_HOURS_DEFAULT = 24

FailureGuardTriggerKind = NewType("FailureGuardTriggerKind", str)
FailureGuardResetEvent = Literal["signature_change", "success"]

CONSECUTIVE_TRIGGER_KIND = FailureGuardTriggerKind("consecutive")
ROLLING_WINDOW_TRIGGER_KIND = FailureGuardTriggerKind("rolling_window")

_RESERVED_PUBLIC_DETAIL_KEYS = frozenset({"kind", "alerted_at", "crossed"})
_FAILURE_MEMORY_ADDRESS_RE = re.compile(r"\b0x[0-9a-f]+\b", re.IGNORECASE)
_FAILURE_OBJECT_REPR_RE = re.compile(
    r"<(?P<label>(?:(?:async_)?generator|coroutine) object [^<>\n]+?"
    r"|[A-Za-z_][\w.]* object) at 0x[0-9a-f]+>",
    re.IGNORECASE,
)
_FAILURE_TASK_ID_RE = re.compile(r"\bTask-\d+\b")
_FAILURE_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_FAILURE_TIMESTAMP_RE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"
)
_FAILURE_RUNTIME_ID_RE = re.compile(
    r"\b(?P<label>pid|process_id|thread_id)(?P<separator>\s*[:=]\s*)\d+\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class FailureGuardTriggerResult:
    """One trigger's evaluated state and trigger-owned public presentation."""

    active: bool
    public_details: Mapping[str, Any]
    alert_title: str
    alert_summary: str

    def __post_init__(self) -> None:
        conflicts = _RESERVED_PUBLIC_DETAIL_KEYS.intersection(self.public_details)
        if conflicts:
            raise ValueError(
                "Failure-guard trigger details use reserved keys: "
                + ", ".join(sorted(conflicts))
            )


class FailureGuardTrigger(Protocol):
    """Behavior supplied by one independently shaped failure trigger."""

    kind: FailureGuardTriggerKind

    async def evaluate(
        self,
        session: AsyncSession,
        job: SchedulerJob,
        now: datetime,
    ) -> FailureGuardTriggerResult:
        """Evaluate the trigger and construct its public/alert details."""

    async def should_reset(
        self,
        session: AsyncSession,
        job: SchedulerJob,
        now: datetime,
        *,
        event: FailureGuardResetEvent,
    ) -> bool:
        """Return whether this trigger's latch should reset for an event."""


@dataclass(frozen=True)
class FailureGuardRegistry:
    """The complete set of triggers evaluated for scheduler jobs."""

    triggers: tuple[FailureGuardTrigger, ...]

    def __post_init__(self) -> None:
        kinds = [str(trigger.kind) for trigger in self.triggers]
        if any(not kind for kind in kinds):
            raise ValueError("Failure-guard trigger kinds must not be empty")
        if len(kinds) != len(set(kinds)):
            raise ValueError("Failure-guard trigger kinds must be unique")


@dataclass(frozen=True)
class FailureGuardEdge:
    """Generic latch and crossing metadata around trigger-owned details."""

    kind: FailureGuardTriggerKind
    public_details: Mapping[str, Any]
    alerted_at: datetime | None
    crossed: bool
    alert_title: str
    alert_summary: str


@dataclass(frozen=True)
class FailureGuardEvaluation:
    """Failure state plus every registered trigger edge."""

    failure_signature: str | None
    last_error: str | None
    edges: tuple[FailureGuardEdge, ...]

    @property
    def crossed_edges(self) -> tuple[FailureGuardEdge, ...]:
        return tuple(edge for edge in self.edges if edge.crossed)


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


def _positive_int_setting(name: str, default: int) -> int:
    try:
        configured = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        configured = default
    return max(1, configured)


def scheduler_failure_guard_registry() -> FailureGuardRegistry:
    """Load the configured registry of independently owned triggers."""
    return FailureGuardRegistry(
        triggers=tuple(
            provide_trigger()
            for provide_trigger in _FAILURE_GUARD_TRIGGER_PROVIDERS
        )
    )


def normalize_scheduler_failure_identity(failure_identity: str) -> str:
    """Remove volatile runtime tokens before identifying a failure streak."""
    normalized = str(failure_identity or "").strip()
    normalized = _FAILURE_OBJECT_REPR_RE.sub(
        lambda match: f"<{match.group('label')}>",
        normalized,
    )
    normalized = _FAILURE_MEMORY_ADDRESS_RE.sub("0x<address>", normalized)
    normalized = _FAILURE_TASK_ID_RE.sub("Task-<id>", normalized)
    normalized = _FAILURE_UUID_RE.sub("<uuid>", normalized)
    normalized = _FAILURE_TIMESTAMP_RE.sub("<timestamp>", normalized)
    normalized = _FAILURE_RUNTIME_ID_RE.sub(
        lambda match: f"{match.group('label')}{match.group('separator')}<id>",
        normalized,
    )
    return "\n".join(line.rstrip() for line in normalized.splitlines())


def scheduler_failure_signature(failure_identity: str) -> str:
    """Return a stable digest for one normalized scheduler failure class."""
    normalized = normalize_scheduler_failure_identity(failure_identity)
    return sha256(normalized.encode("utf-8")).hexdigest()


def serialize_failure_guard(
    evaluation: FailureGuardEvaluation,
) -> dict[str, Any]:
    """Return the canonical public failure-guard payload."""
    return {
        "failure_signature": evaluation.failure_signature,
        "last_error": evaluation.last_error,
        "triggers": [
            {
                "kind": str(edge.kind),
                **edge.public_details,
                "alerted_at": (
                    edge.alerted_at.isoformat() if edge.alerted_at else None
                ),
                "crossed": edge.crossed,
            }
            for edge in evaluation.edges
        ],
    }


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


async def _async_evaluate_failure_guard_triggers(
    session: AsyncSession,
    job: SchedulerJob,
    *,
    now: datetime,
    registry: FailureGuardRegistry,
    latches: dict[FailureGuardTriggerKind, SchedulerFailureGuardLatch],
    persist_crossings: bool,
) -> FailureGuardEvaluation:
    edges: list[FailureGuardEdge] = []
    for trigger in registry.triggers:
        result = await trigger.evaluate(session, job, now)
        latch = latches.get(trigger.kind)
        crossed = persist_crossings and result.active and latch is None
        if crossed:
            latch = SchedulerFailureGuardLatch(
                job_id=job.id,
                trigger_kind=str(trigger.kind),
                alerted_at=now,
            )
            session.add(latch)
            latches[trigger.kind] = latch
        edges.append(
            FailureGuardEdge(
                kind=trigger.kind,
                public_details=dict(result.public_details),
                alerted_at=latch.alerted_at if latch is not None else None,
                crossed=crossed,
                alert_title=result.alert_title,
                alert_summary=result.alert_summary,
            )
        )
    return FailureGuardEvaluation(
        failure_signature=job.failure_signature,
        last_error=job.last_failure_error,
        edges=tuple(edges),
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
    latches = await _async_failure_guard_latches(session, job.id)
    return await _async_evaluate_failure_guard_triggers(
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
    signature = scheduler_failure_signature(failure_identity)
    if locked_job.failure_signature == signature:
        locked_job.consecutive_failure_count = int(
            locked_job.consecutive_failure_count or 0
        ) + 1
    else:
        locked_job.failure_signature = signature
        locked_job.consecutive_failure_count = 1
        reset_latch = False
        for trigger in registry.triggers:
            latch = latches.get(trigger.kind)
            if latch is None or not await trigger.should_reset(
                session,
                locked_job,
                now,
                event="signature_change",
            ):
                continue
            await session.delete(latch)
            del latches[trigger.kind]
            reset_latch = True
        if reset_latch:
            await session.flush()

    locked_job.last_failure_error = error_text
    evaluation = await _async_evaluate_failure_guard_triggers(
        session,
        locked_job,
        now=now,
        registry=registry,
        latches=latches,
        persist_crossings=True,
    )

    await session.flush()
    return evaluation


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
    for trigger in registry.triggers:
        latch = latches.get(trigger.kind)
        if latch is None:
            continue
        if await trigger.should_reset(
            session,
            locked_job,
            now,
            event="success",
        ):
            await session.delete(latch)

    locked_job.failure_signature = None
    locked_job.consecutive_failure_count = 0
    locked_job.last_failure_error = None
    await session.flush()
