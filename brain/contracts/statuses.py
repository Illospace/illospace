"""Import-safe status value contracts shared across layers."""

from __future__ import annotations

import re
from enum import StrEnum

IDEA_STATUS_VALUES = (
    "emerged",
    "queued",
    "active",
    "working",
    "needs_input",
    "unread_reply",
    "blocked",
    "failed",
    "resolved",
    "stale",
    "paused",
    "done",
    "archived",
    "exploring",
    "building",
    "testing",
)
INBOUND_EVENT_STATUS_VALUES = (
    "received",
    "processed",
    "review_required",
    "quarantined",
    "failed",
)
STATUS_RECEIVED = "received"
STATUS_PROCESSED = "processed"
STATUS_REVIEW_REQUIRED = "review_required"
STATUS_QUARANTINED = "quarantined"
STATUS_FAILED = "failed"

EXTERNAL_AGENT_TASK_STATUS_VALUES = (
    "queued",
    "claimed",
    "running",
    "submitted",
    "completed",
    "failed",
    "cancelled",
    "canceled",
    "blocked",
    "expired",
)
EXTERNAL_AGENT_TASK_TERMINAL_STATUS_VALUES = (
    "completed",
    "failed",
    "cancelled",
    "canceled",
)


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


RUN_STATUS_VALUES = tuple(status.value for status in RunStatus)
ACTIVE_RUN_STATUS_VALUES = (
    RunStatus.STARTING.value,
    RunStatus.RUNNING.value,
    RunStatus.PAUSED.value,
    RunStatus.VERIFYING.value,
)
OPEN_RUN_STATUS_VALUES = (
    RunStatus.QUEUED.value,
    *ACTIVE_RUN_STATUS_VALUES,
)
PROCESSING_RUN_STATUS_VALUES = (
    RunStatus.STARTING.value,
    RunStatus.RUNNING.value,
    RunStatus.VERIFYING.value,
)
RUN_FAILED_STATUS_VALUE = RunStatus.FAILED.value

# Historical rows may contain these values from older or adjacent run paths.
# The runtime state machine still only transitions through ``RunStatus``.
LEGACY_AGENT_RUN_STATUS_VALUES = ("cancelled", "error", "blocked")
AGENT_RUN_DB_STATUS_VALUES = RUN_STATUS_VALUES + LEGACY_AGENT_RUN_STATUS_VALUES
RUN_STATUS_ALIASES = {
    "cancelled": RunStatus.CANCELED.value,
    "superseded": RunStatus.CANCELED.value,
    "timeout": RUN_FAILED_STATUS_VALUE,
    "error": RUN_FAILED_STATUS_VALUE,
    "blocked": RUN_FAILED_STATUS_VALUE,
}
PROJECTABLE_RUN_STATUS_VALUES = RUN_STATUS_VALUES + tuple(RUN_STATUS_ALIASES)


def project_run_status_value(status: str | None, fallback: str | None = None) -> str:
    raw = str(status or fallback or RunStatus.QUEUED.value).strip().lower()
    if raw in PROJECTABLE_RUN_STATUS_VALUES:
        return RUN_STATUS_ALIASES.get(raw, raw)
    return raw


def idea_status_pattern() -> str:
    return "^(" + "|".join(re.escape(status) for status in IDEA_STATUS_VALUES) + ")$"
