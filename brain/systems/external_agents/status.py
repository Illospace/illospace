"""Canonical external-agent task status values."""

from __future__ import annotations

from brain.contracts.statuses import (
    EXTERNAL_AGENT_TASK_STATUS_VALUES,
    EXTERNAL_AGENT_TASK_TERMINAL_STATUS_VALUES,
)

EXTERNAL_AGENT_TASK_STATUSES = frozenset(EXTERNAL_AGENT_TASK_STATUS_VALUES)
EXTERNAL_AGENT_TASK_TERMINAL_STATUSES = frozenset(EXTERNAL_AGENT_TASK_TERMINAL_STATUS_VALUES)
