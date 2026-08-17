"""Validation and bounded runtime snapshots for Cycle skill references."""
from __future__ import annotations

import json
from typing import Any

from brain.platform.db.repositories.skills import SkillRepository


CYCLE_SKILL_CONTENT_MAX_CHARS = 12_000


def validate_cycle_skill_ids(value: object) -> list[int]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise ValueError("skill_ids must be a list of unique positive integers")
    normalized: list[int] = []
    for skill_id in value:
        if isinstance(skill_id, bool) or not isinstance(skill_id, int) or skill_id < 1:
            raise ValueError("skill_ids must be a list of unique positive integers")
        if skill_id in normalized:
            raise ValueError("skill_ids must be a list of unique positive integers")
        normalized.append(skill_id)
    return normalized


def validate_cycle_prompt(value: str | None, *, skill_ids: list[int]) -> str:
    prompt = str(value or "").strip()
    if not prompt and not skill_ids:
        raise ValueError("prompt is required when skill_ids is empty")
    return prompt


async def async_resolve_cycle_skill_snapshot(session, cycle: Any) -> dict[str, Any] | None:
    skill_ids = validate_cycle_skill_ids(getattr(cycle, "skill_ids", None))
    if not skill_ids:
        return None

    repository = SkillRepository(session)
    resolved: list[dict[str, Any]] = []
    used_chars = 0
    content_truncated = False
    for skill_id in skill_ids:
        skill = await repository.a_get_visible(
            org_id=str(getattr(cycle, "org_id", None) or ""),
            user_id=str(getattr(cycle, "user_id", None) or ""),
            skill_id=skill_id,
        )
        if skill is None or bool(getattr(skill, "archived", False)):
            continue

        content = _skill_content(skill)
        remaining = CYCLE_SKILL_CONTENT_MAX_CHARS - used_chars
        if remaining <= 0:
            content_truncated = True
            break
        truncated = len(content) > remaining
        bounded_content = content[:remaining]
        resolved.append(
            {
                "id": int(skill.id),
                "name": str(skill.name),
                "version": int(getattr(skill, "version", 1) or 1),
                "content": bounded_content,
                "truncated": truncated,
            }
        )
        used_chars += len(bounded_content)
        if truncated:
            content_truncated = True
            break

    return {
        "skill_ids": skill_ids,
        "skills": resolved,
        "content_max_chars": CYCLE_SKILL_CONTENT_MAX_CHARS,
        "content_chars": used_chars,
        "truncated": content_truncated,
    }


def _skill_content(skill: Any) -> str:
    sections = [f"# {skill.name}"]
    description = str(getattr(skill, "description", None) or "").strip()
    if description:
        sections.extend(("## Mission", description))
    sections.extend(("## Procedure", str(getattr(skill, "procedure", None) or "").strip()))
    guardrails = getattr(skill, "guardrails", None) or []
    if guardrails:
        sections.extend(
            (
                "## Guardrails",
                json.dumps(guardrails, sort_keys=True, ensure_ascii=False, default=str),
            )
        )
    return "\n\n".join(sections)


__all__ = [
    "CYCLE_SKILL_CONTENT_MAX_CHARS",
    "async_resolve_cycle_skill_snapshot",
    "validate_cycle_prompt",
    "validate_cycle_skill_ids",
]
