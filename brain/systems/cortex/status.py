"""Canonical Cortex thought status values."""

from __future__ import annotations

from brain.platform.status_contracts import (
    IDEA_STATUS_VALUES,
    idea_status_pattern,
)
IDEA_STATUSES = frozenset(IDEA_STATUS_VALUES)
RUN_ADMISSION_CREATE_STATUSES = frozenset({"queued", "working"})
PROTECTED_IDEA_STATUSES = frozenset({"archived", "resolved"})
EXTERNAL_TASK_STARTABLE_IDEA_STATUSES = frozenset({
    "emerged",
    "needs_input",
    "unread_reply",
    "active",
})
USER_MESSAGE_ACTIVATES_IDEA_STATUSES = frozenset({
    "needs_input",
    "unread_reply",
    "emerged",
})
ASSISTANT_REPLY_UNREAD_IDEA_STATUSES = frozenset({
    "active",
    "working",
    "queued",
})
ASSISTANT_REPLY_BLOCKED_IDEA_STATUSES = frozenset({"resolved"})
ANALYTICS_QUEUED_IDEA_STATUS_VALUES = (
    "queued",
    "active",
    "exploring",
    "building",
    "testing",
)
SUGGESTED_IDEA_STATUS_VALUES = (
    "needs_input",
    "unread_reply",
    "active",
)
