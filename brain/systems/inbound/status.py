"""Canonical inbound event status values."""

from __future__ import annotations

from brain.contracts.statuses import (
    INBOUND_EVENT_STATUS_VALUES,
    STATUS_FAILED,
    STATUS_PROCESSED,
    STATUS_QUARANTINED,
    STATUS_RECEIVED,
    STATUS_REVIEW_REQUIRED,
)

INBOUND_EVENT_STATUSES = frozenset(INBOUND_EVENT_STATUS_VALUES)
