"""Neutral failure identity and latch/edge evaluation primitives."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import re
from typing import Any, Mapping, NewType, Protocol


FailureGuardTriggerKind = NewType("FailureGuardTriggerKind", str)

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
    latch_new_edges: bool,
) -> FailureGuardEvaluation:
    """Apply durable latches to caller-evaluated trigger results."""
    kinds = [str(result.kind) for result in results]
    if len(kinds) != len(set(kinds)):
        raise ValueError("Failure-guard trigger kinds must be unique")

    latches = dict(await store.load_latches())
    edges: list[FailureGuardEdge] = []
    for result in results:
        latch = latches.get(result.kind)
        crossed = latch_new_edges and result.active and latch is None
        if crossed:
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
