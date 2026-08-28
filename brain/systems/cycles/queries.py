"""Shared SQL clauses for selecting Cycle rows.

Kept out of ``common.py`` on purpose: that module holds constants and pure value
helpers and is imported by contract and schema modules that must not inherit an
ORM dependency.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_
from sqlalchemy.sql.elements import ColumnElement

from brain.platform.db.models.cycle import Cycle
from brain.systems.cycles.common import ILLO_LANE_EXECUTOR_BINDING


def due_illo_lane_cycle_clause(
    cutoff: datetime, *, inclusive: bool
) -> ColumnElement[bool]:
    """Select enabled, scheduled illo-lane cycles due at ``cutoff``.

    ``inclusive=True`` compares ``next_run_at <= cutoff``; ``inclusive=False``
    compares ``next_run_at < cutoff``. Callers differ on this boundary and the
    difference is deliberate, so it is always passed explicitly.
    """
    next_run_at_clause = (
        Cycle.next_run_at <= cutoff if inclusive else Cycle.next_run_at < cutoff
    )
    return and_(
        Cycle.deleted_at.is_(None),
        Cycle.enabled.is_(True),
        Cycle.executor_binding == ILLO_LANE_EXECUTOR_BINDING,
        Cycle.next_run_at.is_not(None),
        next_run_at_clause,
    )
