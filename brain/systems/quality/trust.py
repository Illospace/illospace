"""Trust Manager — trust levels with automatic promotion/demotion.

Trust levels:
  0 = PROBATION  — all completions require test output verification
  1 = STANDARD   — code changes require tests, non-code can skip
  2 = TRUSTED    — can skip tests for trivial changes (< 5 lines, config only)

Auto-degrade: 2 failures in a week → drop one level
Auto-upgrade: 5 consecutive verified successes → raise one level
"""

import json
import os
from datetime import datetime, timedelta

from sqlalchemy import text

from brain.platform.db.repositories.unit_of_work import UnitOfWork

TRUST_LEVELS = {0: "PROBATION", 1: "STANDARD", 2: "TRUSTED"}
LEVEL_UP_THRESHOLD = 5
FAILURES_TO_DEGRADE = 2
DEGRADE_WINDOW_DAYS = 7


def get_trust_level() -> dict:
    """Return current trust state with level name."""
    with UnitOfWork() as uow:
        row = uow.session.execute(
            text("SELECT * FROM trust_state LIMIT 1")
        ).mappings().first()
        if not row:
            return {
                "current_level": 0,
                "level_name": "PROBATION",
                "consecutive_clean": 0,
                "total_completions": 0,
            }
    result = dict(row)
    result["level_name"] = TRUST_LEVELS.get(result["current_level"], "UNKNOWN")
    return result


def check_requirements(task_context: dict) -> dict:
    """Check what's required for this task at current trust level.

    Returns: {requires_tests: bool, requires_verification: bool, reason: str}
    """
    trust = get_trust_level()
    level = trust["current_level"]
    involves_code = task_context.get("involves_code", False)
    lines_changed = task_context.get("lines_changed", 999)
    config_only = task_context.get("config_only", False)

    if level == 0:  # PROBATION
        return {
            "requires_tests": True,
            "requires_verification": True,
            "reason": "PROBATION: all completions require test output verification",
        }
    elif level == 1:  # STANDARD
        if involves_code:
            return {
                "requires_tests": True,
                "requires_verification": True,
                "reason": "STANDARD: code changes require tests",
            }
        return {
            "requires_tests": False,
            "requires_verification": False,
            "reason": "STANDARD: non-code task, tests optional",
        }
    else:  # TRUSTED
        if involves_code and lines_changed >= 5 and not config_only:
            return {
                "requires_tests": True,
                "requires_verification": True,
                "reason": "TRUSTED: non-trivial code change requires tests",
            }
        return {
            "requires_tests": False,
            "requires_verification": False,
            "reason": "TRUSTED: trivial change, tests skippable",
        }


def record_outcome(success: bool, caught_by: str = "self") -> dict:
    """Record a task outcome. Returns updated trust state.

    Auto-degrades on failures, auto-upgrades on consecutive successes.
    """
    trust = get_trust_level()
    level = trust["current_level"]
    consecutive = trust.get("consecutive_clean", 0)

    if success and caught_by == "self":
        consecutive += 1
        # Auto-upgrade: 5 consecutive verified successes
        if consecutive >= LEVEL_UP_THRESHOLD and level < 2:
            level += 1
            consecutive = 0
    else:
        consecutive = 0
        # Check recent failures for auto-degrade
        if not success:
            recent_failures = _count_recent_failures()
            if recent_failures + 1 >= FAILURES_TO_DEGRADE and level > 0:
                level = max(0, level - 1)

    # Update DB
    with UnitOfWork() as uow:
        uow.session.execute(text("""
            UPDATE trust_state SET
                current_level = :level,
                consecutive_clean = :consecutive,
                total_completions = total_completions + 1,
                updated_at = NOW()
            WHERE id = (SELECT id FROM trust_state LIMIT 1)
        """), {"level": level, "consecutive": consecutive})

    return {
        "current_level": level,
        "level_name": TRUST_LEVELS.get(level, "UNKNOWN"),
        "consecutive_clean": consecutive,
    }


def _count_recent_failures() -> int:
    """Count failures in the last DEGRADE_WINDOW_DAYS days."""
    with UnitOfWork() as uow:
        row = uow.session.execute(text("""
            SELECT COUNT(*) as cnt FROM violation_log
            WHERE session_date >= CURRENT_DATE - INTERVAL :days
        """), {"days": f"{DEGRADE_WINDOW_DAYS} days"}).mappings().first()
        return row["cnt"] if row else 0
