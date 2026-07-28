"""Shared failure-guard evaluation, latching, serialization, and delivery."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
import os
import re
from typing import Any, Awaitable, Callable, Literal, Mapping, NewType, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from brain.systems.slack.client import slack_web_client_from_runtime


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


def positive_int_setting(name: str, default: int) -> int:
    """Load a positive integer setting with one shared fallback rule."""
    try:
        configured = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        configured = default
    return max(1, configured)


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
    """Mutable failure state shared by every guarded subject."""

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
    """The alert timestamp needed by the generic edge evaluator."""

    alerted_at: datetime


FailureGuardCreateLatch = Callable[
    [FailureGuardTriggerKind, datetime],
    Awaitable[FailureGuardLatch],
]
FailureGuardDeleteLatch = Callable[
    [FailureGuardTriggerKind, FailureGuardLatch],
    Awaitable[None],
]


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
class FailureAlertSubject:
    """Presentation fields for one guarded subject's Slack card."""

    identity_label: str
    identity: str
    url_label: str
    url: str
    link_label: str
    combined_alert_title: str


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


async def _resolve_failure_alert_channel(client: Any, configured: str) -> str:
    channel = str(configured or "").strip()
    if not channel.startswith("#"):
        return channel

    target_name = channel.removeprefix("#")
    cursor: str | None = None
    seen_cursors: set[str] = set()
    while True:
        response = await client.conversations_list(
            types="public_channel,private_channel",
            limit=200,
            cursor=cursor,
            exclude_archived=True,
        )
        for candidate in response.get("channels") or []:
            if not isinstance(candidate, dict):
                continue
            if str(candidate.get("name") or "") == target_name:
                return str(candidate.get("id") or channel)
        metadata = response.get("response_metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        next_cursor = str(metadata.get("next_cursor") or "").strip()
        if not next_cursor or next_cursor in seen_cursors:
            return channel
        seen_cursors.add(next_cursor)
        cursor = next_cursor


async def async_deliver_failure_alert(
    *,
    subject: FailureAlertSubject,
    evaluation: FailureGuardEvaluation,
    error_text: str,
    client_factory: Callable[..., Awaitable[Any]] | None = None,
) -> None:
    """Post all edges crossed by one evaluation as one Slack notification."""
    crossed_edges = evaluation.crossed_edges
    if not crossed_edges:
        raise ValueError("failure-guard alert requires at least one crossed edge")

    provide_client = client_factory or slack_web_client_from_runtime
    client = await provide_client(
        requested_by="scheduler_failure_alert",
        reason="Deliver a repeated scheduler job failure alert to the team.",
    )
    configured_channel = (
        os.getenv("ILLO_SCHEDULER_FAILURE_ALERT_CHANNEL", "").strip()
        or "#alerts"
    )
    channel = await _resolve_failure_alert_channel(client, configured_channel)
    first_error_line = next(
        (line.strip() for line in str(error_text or "").splitlines() if line.strip()),
        "Unknown scheduler failure",
    )
    if len(crossed_edges) == 1:
        alert_title = crossed_edges[0].alert_title
        failure_summary = crossed_edges[0].alert_summary
    else:
        alert_title = subject.combined_alert_title
        failure_summary = "\n".join(
            (
                "Triggers crossed:",
                *(
                    f"- {edge.kind}: {edge.alert_summary}"
                    for edge in crossed_edges
                ),
            )
        )
    await client.post_message(
        channel=channel,
        text=(
            f"{alert_title}\n"
            f"{subject.identity_label}: {subject.identity}\n"
            f"{failure_summary}\n"
            f"Error: {first_error_line}\n"
            f"{subject.url_label}: <{subject.url}|{subject.link_label}>"
        ),
    )


async def async_evaluate_failure_guard(
    session: AsyncSession,
    subject: FailureGuardSubject,
    *,
    now: datetime,
    registry: FailureGuardRegistry,
    latches: dict[FailureGuardTriggerKind, FailureGuardLatch],
    persist_crossings: bool,
    create_latch: FailureGuardCreateLatch | None = None,
) -> FailureGuardEvaluation:
    """Evaluate every registered trigger against one subject state."""
    edges: list[FailureGuardEdge] = []
    for trigger in registry.triggers:
        result = await trigger.evaluate(session, subject, now)
        latch = latches.get(trigger.kind)
        crossed = persist_crossings and result.active and latch is None
        if crossed:
            if create_latch is None:
                raise ValueError("persisted failure-guard evaluation needs a latch writer")
            latch = await create_latch(trigger.kind, now)
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


async def async_record_failure(
    session: AsyncSession,
    subject: FailureGuardSubject,
    *,
    failure_identity: str,
    error_text: str,
    now: datetime,
    registry: FailureGuardRegistry,
    latches: dict[FailureGuardTriggerKind, FailureGuardLatch],
    create_latch: FailureGuardCreateLatch,
    delete_latch: FailureGuardDeleteLatch,
) -> FailureGuardEvaluation:
    """Update one locked subject and evaluate all trigger edges once."""
    signature = failure_signature(failure_identity)
    if subject.failure_signature == signature:
        subject.consecutive_failure_count = int(
            subject.consecutive_failure_count or 0
        ) + 1
    else:
        subject.failure_signature = signature
        subject.consecutive_failure_count = 1
        reset_latch = False
        for trigger in registry.triggers:
            latch = latches.get(trigger.kind)
            if latch is None or not await trigger.should_reset(
                session,
                subject,
                now,
                event="signature_change",
            ):
                continue
            await delete_latch(trigger.kind, latch)
            del latches[trigger.kind]
            reset_latch = True
        if reset_latch:
            await session.flush()

    subject.last_failure_error = error_text
    evaluation = await async_evaluate_failure_guard(
        session,
        subject,
        now=now,
        registry=registry,
        latches=latches,
        persist_crossings=True,
        create_latch=create_latch,
    )
    await session.flush()
    return evaluation


async def async_reset_failure_guard(
    session: AsyncSession,
    subject: FailureGuardSubject,
    *,
    now: datetime,
    registry: FailureGuardRegistry,
    latches: dict[FailureGuardTriggerKind, FailureGuardLatch],
    delete_latch: FailureGuardDeleteLatch,
) -> None:
    """Reset one locked subject after a successful terminal outcome."""
    for trigger in registry.triggers:
        latch = latches.get(trigger.kind)
        if latch is None:
            continue
        if await trigger.should_reset(
            session,
            subject,
            now,
            event="success",
        ):
            await delete_latch(trigger.kind, latch)

    subject.failure_signature = None
    subject.consecutive_failure_count = 0
    subject.last_failure_error = None
    await session.flush()
