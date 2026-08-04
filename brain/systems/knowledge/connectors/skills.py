"""Incremental mirror of procedural skills into Illo Knowledge."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.kernel.config import KNOWLEDGE_CONNECTOR_BATCH_SIZE
from brain.platform.db.models.skill import Skill
from brain.systems.knowledge.connectors.base import (
    KnowledgeDraft,
    KnowledgeEnumeration,
)


def _cursor_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _section(label: str, value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        content = value.strip()
    elif isinstance(value, (list, dict)):
        if not value:
            return ""
        content = json.dumps(
            value,
            default=str,
            ensure_ascii=False,
            sort_keys=True,
        )
    else:
        content = str(value).strip()
    return f"{label}:\n{content}" if content else ""


def _skill_raw_text(skill: Skill) -> str:
    sections = (
        _section("Description", skill.description),
        _section("Procedure", skill.procedure),
        _section("Trigger patterns", skill.triggers),
        _section("Guardrails", skill.guardrails),
        _section("Pitfalls", skill.pitfalls),
    )
    return "\n\n".join(section for section in sections if section)


def _draft_for_skill(skill: Skill) -> KnowledgeDraft:
    raw_text = _skill_raw_text(skill)
    archived = bool(skill.archived)
    return KnowledgeDraft(
        source="skills",
        kind="skill",
        source_ref=f"skill:{skill.id}",
        title=str(skill.name).strip(),
        summary=raw_text,
        entities=list(
            dict.fromkeys(
                (
                    str(skill.skill_type or "skill").strip(),
                    str(skill.maturity or "emerging").strip(),
                )
            )
        ),
        raw_text=raw_text,
        extra={
            "archived": archived,
            "maturity": skill.maturity,
            "skill_type": skill.skill_type,
            "skill_view": {
                "tool": "skill_view",
                "arguments": {"name": skill.name},
            },
            "success_count": int(skill.success_count or 0),
            "use_count": int(skill.use_count or 0),
            "version": int(skill.version or 1),
        },
        source_created_at=skill.created_at,
        source_updated_at=skill.updated_at,
        archived_at=skill.updated_at if archived else None,
    )


class SkillsConnector:
    """Enumerate skills by a stable update watermark."""

    source_key = "skills"

    def __init__(self, *, max_items: int = KNOWLEDGE_CONNECTOR_BATCH_SIZE):
        self.max_items = max(1, int(max_items))

    async def enumerate_changed(
        self,
        session: AsyncSession,
        cursor: dict[str, Any],
    ) -> KnowledgeEnumeration:
        marker = _cursor_datetime(cursor.get("updated_at"))
        marker_id = max(0, int(cursor.get("id") or 0))
        statement = (
            select(Skill)
            .order_by(Skill.updated_at.asc(), Skill.id.asc())
            .limit(self.max_items)
        )
        if marker is not None:
            statement = statement.where(
                or_(
                    Skill.updated_at > marker,
                    and_(Skill.updated_at == marker, Skill.id > marker_id),
                )
            )

        rows = list((await session.scalars(statement)).all())
        if not rows:
            return KnowledgeEnumeration(drafts=[], cursor=dict(cursor))
        last = rows[-1]
        return KnowledgeEnumeration(
            drafts=[_draft_for_skill(skill) for skill in rows],
            cursor={
                "updated_at": _utc_iso(last.updated_at),
                "id": last.id,
            },
        )


__all__ = ["SkillsConnector"]
