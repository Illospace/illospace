"""Shared scheduler freeze thresholds."""
from __future__ import annotations

from datetime import timedelta

from brain.kernel.common.env import env_int


def scheduler_self_heal_after() -> timedelta:
    """Return the overdue duration used by self-heal health policy."""
    return timedelta(
        minutes=env_int(
            "SCHEDULER_SELF_HEAL_AFTER_MINUTES",
            10,
            minimum=1,
        )
    )


def scheduler_self_heal_max_attempts() -> int:
    """Return the maximum automatic restart attempts per freeze episode."""
    return env_int(
        "SCHEDULER_SELF_HEAL_MAX_ATTEMPTS",
        2,
        minimum=1,
    )


__all__ = [
    "scheduler_self_heal_after",
    "scheduler_self_heal_max_attempts",
]
