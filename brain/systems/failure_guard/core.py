"""Neutral failure observation, edge evaluation, and latch persistence."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import re
from typing import Any, Literal, Mapping, NewType, Protocol

from sqlalchemy.ext.asyncio import AsyncSession


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
class FailureObservation:
    """Typed data describing one failure before guard state is changed."""

    classification: str
    signature_input: str
    error_text: str
    alert_threshold: int
    alert_class: str
    operator_action: str | None

    def __post_init__(self) -> None:
        if not self.classification.strip():
            raise ValueError("failure classification must not be empty")
        if not self.signature_input.strip():
            raise ValueError("failure signature input must not be empty")
        if self.alert_threshold < 1:
            raise ValueError("failure alert threshold must be positive")
        if not self.alert_class.strip():
            raise ValueError("failure alert class must not be empty")
        if self.operator_action is not None and not self.operator_action.strip():
            raise ValueError("failure operator action must not be empty")


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


class FailureGuardSubject(Protocol):
    """Mutable state required by every guarded subject."""

    id: int
    failure_signature: str | None
    consecutive_failure_count: int
    last_failure_error: str | None


class FailureGuardTrigger(Protocol):
    """Behavior supplied by one independently shaped failure trigger."""

    kind: FailureGuardTriggerKind

    async def evaluate(
        self,
        session: AsyncSession,
        subject: FailureGuardSubject,
        now: datetime,
        *,
        observation: FailureObservation | None = None,
    ) -> FailureGuardTriggerResult:
        """Evaluate the trigger and construct its public/alert details."""

    async def should_reset(
        self,
        session: AsyncSession,
        subject: FailureGuardSubject,
        now: datetime,
        *,
        event: FailureGuardResetEvent,
    ) -> bool:
        """Return whether this trigger's latch should reset for an event."""


@dataclass(frozen=True)
class FailureGuardRegistry:
    """The complete set of triggers evaluated for one subject class."""

    triggers: tuple[FailureGuardTrigger, ...]

    def __post_init__(self) -> None:
        kinds = [str(trigger.kind) for trigger in self.triggers]
        if any(not kind for kind in kinds):
            raise ValueError("Failure-guard trigger kinds must not be empty")
        if len(kinds) != len(set(kinds)):
            raise ValueError("Failure-guard trigger kinds must be unique")


class FailureGuardLatch(Protocol):
    """The alert timestamp required by the neutral edge evaluator."""

    alerted_at: datetime


class FailureGuardStore(Protocol):
    """Persistence boundary shared by every failure-guard adapter."""

    async def load_latches(
        self,
    ) -> Mapping[FailureGuardTriggerKind, FailureGuardLatch]:
        """Return all persisted trigger latches for one guarded subject."""

    async def create_latch(
        self,
        trigger_kind: FailureGuardTriggerKind,
        alerted_at: datetime,
    ) -> FailureGuardLatch:
        """Persist and return one trigger latch."""

    async def delete_latch(
        self,
        trigger_kind: FailureGuardTriggerKind,
    ) -> None:
        """Delete one trigger latch when present."""


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


def normalize_failure_identity(failure_identity: str) -> str:
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


def failure_signature(failure_identity: str) -> str:
    """Return a stable digest for one normalized failure class."""
    normalized = normalize_failure_identity(failure_identity)
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


async def _async_evaluate_edges(
    session: AsyncSession,
    subject: FailureGuardSubject,
    *,
    now: datetime,
    registry: FailureGuardRegistry,
    latches: dict[FailureGuardTriggerKind, FailureGuardLatch],
    store: FailureGuardStore | None,
    observation: FailureObservation | None,
) -> FailureGuardEvaluation:
    edges: list[FailureGuardEdge] = []
    for trigger in registry.triggers:
        result = await trigger.evaluate(
            session,
            subject,
            now,
            observation=observation,
        )
        latch = latches.get(trigger.kind)
        crossed = store is not None and result.active and latch is None
        if crossed:
            latch = await store.create_latch(trigger.kind, now)
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
        failure_signature=subject.failure_signature,
        last_error=subject.last_failure_error,
        edges=tuple(edges),
    )


async def async_read_failure_guard(
    session: AsyncSession,
    subject: FailureGuardSubject,
    *,
    now: datetime,
    registry: FailureGuardRegistry,
    store: FailureGuardStore,
) -> FailureGuardEvaluation:
    """Evaluate current trigger state without creating new latches."""
    latches = dict(await store.load_latches())
    return await _async_evaluate_edges(
        session,
        subject,
        now=now,
        registry=registry,
        latches=latches,
        store=None,
        observation=None,
    )


async def async_record_failure(
    session: AsyncSession,
    subject: FailureGuardSubject,
    *,
    observation: FailureObservation,
    now: datetime,
    registry: FailureGuardRegistry,
    store: FailureGuardStore,
) -> FailureGuardEvaluation:
    """Update one locked subject and persist trigger crossings once."""
    latches = dict(await store.load_latches())
    signature = failure_signature(observation.signature_input)
    if subject.failure_signature == signature:
        subject.consecutive_failure_count = int(
            subject.consecutive_failure_count or 0
        ) + 1
    else:
        subject.failure_signature = signature
        subject.consecutive_failure_count = 1
        reset_latch = False
        for trigger in registry.triggers:
            if trigger.kind not in latches or not await trigger.should_reset(
                session,
                subject,
                now,
                event="signature_change",
            ):
                continue
            await store.delete_latch(trigger.kind)
            del latches[trigger.kind]
            reset_latch = True
        if reset_latch:
            await session.flush()

    subject.last_failure_error = observation.error_text
    evaluation = await _async_evaluate_edges(
        session,
        subject,
        now=now,
        registry=registry,
        latches=latches,
        store=store,
        observation=observation,
    )
    await session.flush()
    return evaluation


async def async_reset_failure_guard(
    session: AsyncSession,
    subject: FailureGuardSubject,
    *,
    now: datetime,
    registry: FailureGuardRegistry,
    store: FailureGuardStore,
) -> None:
    """Reset one locked subject after a successful terminal outcome."""
    latches = dict(await store.load_latches())
    for trigger in registry.triggers:
        if trigger.kind not in latches:
            continue
        if await trigger.should_reset(
            session,
            subject,
            now,
            event="success",
        ):
            await store.delete_latch(trigger.kind)

    subject.failure_signature = None
    subject.consecutive_failure_count = 0
    subject.last_failure_error = None
    await session.flush()
