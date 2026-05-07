"""Canonical run status model."""

from __future__ import annotations

from enum import StrEnum


class RunStatus(StrEnum):
    QUEUED = "queued"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    EXPIRED = "expired"


TERMINAL_RUN_STATUSES = frozenset({
    RunStatus.COMPLETED,
    RunStatus.FAILED,
    RunStatus.CANCELED,
    RunStatus.EXPIRED,
})

ACTIVE_RUN_STATUSES = frozenset({
    RunStatus.STARTING,
    RunStatus.RUNNING,
    RunStatus.PAUSED,
    RunStatus.VERIFYING,
})

RESUMABLE_RUN_STATUSES = ACTIVE_RUN_STATUSES

ALLOWED_RUN_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.QUEUED: frozenset({RunStatus.STARTING, RunStatus.CANCELED, RunStatus.EXPIRED, RunStatus.FAILED}),
    RunStatus.STARTING: frozenset({RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELED, RunStatus.EXPIRED}),
    RunStatus.RUNNING: frozenset({RunStatus.PAUSED, RunStatus.VERIFYING, RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELED, RunStatus.EXPIRED}),
    RunStatus.PAUSED: frozenset({RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELED, RunStatus.EXPIRED}),
    RunStatus.VERIFYING: frozenset({RunStatus.RUNNING, RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELED, RunStatus.EXPIRED}),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELED: frozenset(),
    RunStatus.EXPIRED: frozenset(),
}


class RunTransitionError(RuntimeError):
    """Raised when a run attempts an undefined state change."""


def coerce_run_status(value: str | RunStatus | None, *, default: RunStatus = RunStatus.QUEUED) -> RunStatus:
    if isinstance(value, RunStatus):
        return value
    candidate = str(value or "").strip().lower()
    for status in RunStatus:
        if candidate == status.value:
            return status
    return default


def ensure_run_transition(from_status: str | RunStatus | None, to_status: str | RunStatus) -> tuple[RunStatus, RunStatus]:
    current = coerce_run_status(from_status)
    target = coerce_run_status(to_status)
    if target == current:
        return current, target
    if target not in ALLOWED_RUN_TRANSITIONS[current]:
        raise RunTransitionError(f"Invalid run transition {current.value!r} -> {target.value!r}")
    return current, target


__all__ = [
    "ALLOWED_RUN_TRANSITIONS",
    "ACTIVE_RUN_STATUSES",
    "RESUMABLE_RUN_STATUSES",
    "RunStatus",
    "RunTransitionError",
    "TERMINAL_RUN_STATUSES",
    "coerce_run_status",
    "ensure_run_transition",
]
