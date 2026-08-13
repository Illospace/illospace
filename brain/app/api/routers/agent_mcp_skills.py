"""Skill read capabilities for the hosted MCP endpoint."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.repositories.skills import SkillRepository
from brain.platform.db.schemas.skills import SkillAgentRead, SkillAgentSummary
from brain.systems.external_agents import service as external_agents


READ_CAPABILITIES: dict[str, dict[str, Any]] = {
    "skills.get": {
        "description": "Read one active stored Illo skill visible to the bridge user by stable id or exact name.",
        "arguments": {
            "skill_id": "integer",
            "name": "string",
        },
    },
    "skills.list": {
        "description": (
            "List active stored Illo skill ids, names, versions, and archive status "
            "visible to the bridge user."
        ),
        "arguments": {},
    },
}


def _clean_optional_string(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _clean_optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


async def read_skill(
    db: AsyncSession,
    principal: external_agents.AgentBridgePrincipal,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    skill_id = _clean_optional_int(arguments.get("skill_id"))
    name = _clean_optional_string(arguments.get("name"))
    if (skill_id is None) == (name is None):
        raise ValueError("skills.get requires exactly one of skill_id or name")
    skill = await SkillRepository(db).a_get_visible(
        org_id=principal.org_id,
        user_id=principal.owner_user_id,
        skill_id=skill_id,
        name=name,
    )
    if skill is None:
        raise ValueError("Skill not found")
    return {"skill": SkillAgentRead.model_validate(skill).model_dump(mode="json")}


async def list_skills(
    db: AsyncSession,
    principal: external_agents.AgentBridgePrincipal,
) -> dict[str, Any]:
    skills = await SkillRepository(db).a_list_visible(
        org_id=principal.org_id,
        user_id=principal.owner_user_id,
    )
    return {
        "skills": [
            SkillAgentSummary.model_validate(skill).model_dump(mode="json")
            for skill in skills
        ]
    }


__all__ = [
    "READ_CAPABILITIES",
    "list_skills",
    "read_skill",
]
