"""Neutral failure identity and latch/edge evaluation primitives."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from hashlib import sha256
import re
from typing import (
    Any,
    Generic,
    Literal,
    Mapping,
    NewType,
    Protocol,
    Sequence,
    TypeAlias,
    TypeVar,
    cast,
)


FailureGuardTriggerKind = NewType("FailureGuardTriggerKind", str)
FailureGuardLifecycleEvent = Literal[
    "new_failure",
    "repeated_failure",
    "success",
]
FailureGuardNewEdgeMode = Literal["ignore", "detect", "latch"]
JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
FailureGuardTriggerState: TypeAlias = Mapping[str, JsonValue]
FailureGuardContextT = TypeVar("FailureGuardContextT")

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
class FailureRecord:
    """The caller-independent identity and error for one failure."""

    signature_input: str
    error_text: str

    def __post_init__(self) -> None:
        if not self.signature_input.strip():
            raise ValueError("failure signature input must not be empty")


@dataclass(frozen=True)
class FailureGuardTriggerResult:
    """One already-evaluated trigger with caller-owned presentation."""

    kind: FailureGuardTriggerKind
    active: bool
    public_details: Mapping[str, Any]
    alert_title: str
    alert_summary: str

    def __post_init__(self) -> None:
        if not str(self.kind):
            raise ValueError("failure-guard trigger kind must not be empty")
        conflicts = _RESERVED_PUBLIC_DETAIL_KEYS.intersection(self.public_details)
        if conflicts:
            raise ValueError(
                "Failure-guard trigger details use reserved keys: "
                + ", ".join(sorted(conflicts))
            )


class FailureGuardLatch(Protocol):
    """The alert timestamp required by the neutral edge evaluator."""

    alerted_at: datetime


class FailureGuardStore(Protocol):
    """Persistence boundary used by the latch/edge evaluator."""

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


class FailureGuardStateStore(Protocol):
    """Generic mutable state persistence for registered triggers."""

    async def load_trigger_states(
        self,
    ) -> Mapping[FailureGuardTriggerKind, FailureGuardTriggerState]:
        """Return the persisted state for every stateful trigger."""

    async def save_trigger_state(
        self,
        trigger_kind: FailureGuardTriggerKind,
        state: FailureGuardTriggerState,
    ) -> None:
        """Persist one trigger's complete state document."""

    async def delete_trigger_state(
        self,
        trigger_kind: FailureGuardTriggerKind,
    ) -> None:
        """Delete one trigger's state while preserving any active latch."""


class FailureGuardTrigger(Protocol[FailureGuardContextT]):
    """A stateless trigger evaluated from consumer-owned context."""

    kind: FailureGuardTriggerKind

    async def evaluate(
        self,
        context: FailureGuardContextT,
    ) -> FailureGuardTriggerResult:
        """Evaluate the trigger and construct its public presentation."""


class FailureGuardStatefulTrigger(Protocol[FailureGuardContextT]):
    """Complete contract for a trigger that owns persisted JSON state."""

    kind: FailureGuardTriggerKind

    async def evaluate_with_state(
        self,
        context: FailureGuardContextT,
        *,
        state: FailureGuardTriggerState,
    ) -> FailureGuardTriggerResult:
        """Evaluate the trigger from its complete persisted state."""

    async def transition_state(
        self,
        context: FailureGuardContextT,
        state: FailureGuardTriggerState,
        *,
        event: FailureGuardLifecycleEvent,
    ) -> FailureGuardTriggerState | None:
        """Return replacement state, or ``None`` to clear persisted state."""


class FailureGuardTriggerMode(StrEnum):
    """A registered trigger's explicit evaluation and persistence mode."""

    STATELESS = "stateless"
    STATEFUL = "stateful"


@dataclass(frozen=True)
class FailureGuardTriggerRegistration(Generic[FailureGuardContextT]):
    """Bind one trigger implementation to its declared dispatch mode."""

    mode: FailureGuardTriggerMode
    trigger: (
        FailureGuardTrigger[FailureGuardContextT]
        | FailureGuardStatefulTrigger[FailureGuardContextT]
    )

    def __post_init__(self) -> None:
        trigger_name = type(self.trigger).__name__
        if not isinstance(self.mode, FailureGuardTriggerMode):
            raise ValueError(
                f"Failure-guard trigger {trigger_name} has invalid mode: "
                f"{self.mode!r}"
            )
        stateless_method = "evaluate"
        stateful_methods = ("evaluate_with_state", "transition_state")
        if self.mode is FailureGuardTriggerMode.STATEFUL:
            missing_methods = [
                method
                for method in stateful_methods
                if not callable(getattr(self.trigger, method, None))
            ]
            if missing_methods:
                raise ValueError(
                    f"Failure-guard trigger {trigger_name} declared stateful "
                    "but is missing required method(s): "
                    + ", ".join(missing_methods)
                )
            return

        if not callable(getattr(self.trigger, stateless_method, None)):
            raise ValueError(
                f"Failure-guard trigger {trigger_name} declared stateless "
                f"but is missing required method: {stateless_method}"
            )
        surplus_methods = [
            method
            for method in stateful_methods
            if callable(getattr(self.trigger, method, None))
        ]
        if surplus_methods:
            raise ValueError(
                f"Failure-guard trigger {trigger_name} declared stateless "
                "but has surplus stateful method(s): "
                + ", ".join(surplus_methods)
            )

    @property
    def kind(self) -> FailureGuardTriggerKind:
        return self.trigger.kind


def require_failure_guard_registrations(
    triggers: Sequence[object], *, owner: str
) -> None:
    """Reject a registry entry that is a bare trigger rather than a registration.

    Without this, a raw trigger passes a consumer registry's own checks — it has
    a ``kind`` — and skips ``FailureGuardTriggerRegistration.__post_init__``
    entirely. The declared mode would then be missing at dispatch time, far from
    the provider that omitted it, which is exactly the late failure this module
    exists to prevent.
    """

    unregistered = [
        type(trigger).__name__
        for trigger in triggers
        if not isinstance(trigger, FailureGuardTriggerRegistration)
    ]
    if unregistered:
        raise ValueError(
            f"{owner} failure-guard triggers must be wrapped in "
            "FailureGuardTriggerRegistration with an explicit mode; got bare "
            "trigger(s): " + ", ".join(unregistered)
        )


async def async_evaluate_failure_guard_triggers(
    *,
    triggers: Sequence[FailureGuardTriggerRegistration[FailureGuardContextT]],
    context: FailureGuardContextT,
    store: FailureGuardStateStore,
) -> tuple[FailureGuardTriggerResult, ...]:
    """Evaluate registered triggers through their declared public contract."""
    states = dict(await store.load_trigger_states())
    results: list[FailureGuardTriggerResult] = []
    for registration in triggers:
        trigger = registration.trigger
        if registration.mode is FailureGuardTriggerMode.STATEFUL:
            stateful_trigger = cast(
                FailureGuardStatefulTrigger[FailureGuardContextT],
                trigger,
            )
            results.append(
                await stateful_trigger.evaluate_with_state(
                    context,
                    state=dict(states.get(registration.kind, {})),
                )
            )
        else:
            stateless_trigger = cast(
                FailureGuardTrigger[FailureGuardContextT],
                trigger,
            )
            results.append(await stateless_trigger.evaluate(context))
    return tuple(results)


async def async_transition_failure_guard_trigger_states(
    *,
    triggers: Sequence[FailureGuardTriggerRegistration[FailureGuardContextT]],
    context: FailureGuardContextT,
    event: FailureGuardLifecycleEvent,
    store: FailureGuardStateStore,
) -> None:
    """Apply one lifecycle event to every registered state-owning trigger."""
    states = dict(await store.load_trigger_states())
    for registration in triggers:
        if registration.mode is FailureGuardTriggerMode.STATELESS:
            continue
        trigger = cast(
            FailureGuardStatefulTrigger[FailureGuardContextT],
            registration.trigger,
        )
        next_state = await trigger.transition_state(
            context,
            dict(states.get(registration.kind, {})),
            event=event,
        )
        if next_state is None:
            await store.delete_trigger_state(registration.kind)
        else:
            await store.save_trigger_state(registration.kind, dict(next_state))


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


async def async_evaluate_failure_edges(
    *,
    results: tuple[FailureGuardTriggerResult, ...],
    failure_signature: str | None,
    last_error: str | None,
    now: datetime,
    store: FailureGuardStore,
    new_edge_mode: FailureGuardNewEdgeMode,
) -> FailureGuardEvaluation:
    """Detect new edges and optionally persist their durable latches."""
    kinds = [str(result.kind) for result in results]
    if len(kinds) != len(set(kinds)):
        raise ValueError("Failure-guard trigger kinds must be unique")

    latches = dict(await store.load_latches())
    edges: list[FailureGuardEdge] = []
    for result in results:
        latch = latches.get(result.kind)
        crossed = (
            new_edge_mode != "ignore" and result.active and latch is None
        )
        if crossed and new_edge_mode == "latch":
            latch = await store.create_latch(result.kind, now)
            latches[result.kind] = latch
        edges.append(
            FailureGuardEdge(
                kind=result.kind,
                public_details=dict(result.public_details),
                alerted_at=latch.alerted_at if latch is not None else None,
                crossed=crossed,
                alert_title=result.alert_title,
                alert_summary=result.alert_summary,
            )
        )
    return FailureGuardEvaluation(
        failure_signature=failure_signature,
        last_error=last_error,
        edges=tuple(edges),
    )


async def async_latch_failure_edges(
    evaluation: FailureGuardEvaluation,
    *,
    alerted_at: datetime,
    store: FailureGuardStore,
) -> FailureGuardEvaluation:
    """Persist previously detected edges after alert delivery succeeds."""
    edges: list[FailureGuardEdge] = []
    for edge in evaluation.edges:
        if not edge.crossed or edge.alerted_at is not None:
            edges.append(edge)
            continue
        latch = await store.create_latch(edge.kind, alerted_at)
        edges.append(replace(edge, alerted_at=latch.alerted_at))
    return replace(evaluation, edges=tuple(edges))
