"""Import-safe status value contracts shared across layers."""

from __future__ import annotations

import re

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

RUN_STATUS_VALUES = (
    "queued",
    "starting",
    "running",
    "paused",
    "verifying",
    "completed",
    "failed",
    "canceled",
    "expired",
)
ACTIVE_RUN_STATUS_VALUES = (
    "starting",
    "running",
    "paused",
    "verifying",
)
OPEN_RUN_STATUS_VALUES = (
    "queued",
    *ACTIVE_RUN_STATUS_VALUES,
)
PROCESSING_RUN_STATUS_VALUES = (
    "starting",
    "running",
    "verifying",
)
RUN_FAILED_STATUS_VALUE = "failed"

# Historical rows may contain these values from older or adjacent run paths.
# The runtime state machine still only transitions through ``RunStatus``.
LEGACY_AGENT_RUN_STATUS_VALUES = ("cancelled", "error", "blocked")
AGENT_RUN_DB_STATUS_VALUES = RUN_STATUS_VALUES + LEGACY_AGENT_RUN_STATUS_VALUES
RUN_STATUS_ALIASES = {
    "cancelled": "canceled",
    "superseded": "canceled",
    "timeout": RUN_FAILED_STATUS_VALUE,
    "error": RUN_FAILED_STATUS_VALUE,
}
PROJECTABLE_RUN_STATUS_VALUES = RUN_STATUS_VALUES + tuple(RUN_STATUS_ALIASES)


def project_run_status_value(status: str | None, fallback: str | None = None) -> str:
    raw = str(status or fallback or "queued").strip().lower()
    if raw in PROJECTABLE_RUN_STATUS_VALUES:
        return RUN_STATUS_ALIASES.get(raw, raw)
    return raw


def idea_status_pattern() -> str:
    return "^(" + "|".join(re.escape(status) for status in IDEA_STATUS_VALUES) + ")$"
