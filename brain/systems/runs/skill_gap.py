"""Skill-gap runtime annotations."""

from __future__ import annotations

import logging


logger = logging.getLogger("agent_runtime")


def handle_flag_skill_gap(task_domain: str, closest_skill: str, suggested_skill_name: str) -> dict:
    """Acknowledge that no current skill covers the requested domain."""
    logger.info(
        "skill_gap_flagged domain=%s closest=%s suggested=%s",
        task_domain,
        closest_skill,
        suggested_skill_name,
    )
    return {
        "gap_acknowledged": True,
        "action": (
            f"Consider creating a skill called '{suggested_skill_name}' for the '{task_domain}' domain later, "
            f"outside the live agent tool surface, with a clear procedure, pitfalls, and triggers. "
            f"Closest current skill: {closest_skill}."
        ),
    }


__all__ = ["handle_flag_skill_gap"]
