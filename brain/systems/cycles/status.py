"""Canonical cycle-run status values."""

from __future__ import annotations


CYCLE_RUN_ACTIVE_STATUS_VALUES = ("queued", "running", "pending_approval")
CYCLE_RUN_TERMINAL_STATUS_VALUES = (
    "completed",
    "failed",
    "skipped",
    "degraded",
    "auth_blocked",
    "quota_blocked",
)

CYCLE_RUN_ACTIVE_STATUSES = frozenset(CYCLE_RUN_ACTIVE_STATUS_VALUES)
CYCLE_RUN_TERMINAL_STATUSES = frozenset(CYCLE_RUN_TERMINAL_STATUS_VALUES)
