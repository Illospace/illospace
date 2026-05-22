"""Guardrail MCP tool implementation."""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text


async def brain_guardrails_tool(
    skill: str | None = None,
    *,
    unit_of_work_cls: Any,
    maybe_await: Any,
    session_execute: Any,
) -> dict:
    """Get guardrails: recent failures, high-salience warnings, and skill-specific pitfalls."""
    result = {"guardrails": [], "warnings": [], "pitfalls": []}

    async with unit_of_work_cls() as uow:
        rows_result = await session_execute(uow.session, text("""
            SELECT s.name, se.outcome_details, se.error_analysis, se.started_at
            FROM skill_executions se
            JOIN skills s ON s.id = se.skill_id
            WHERE se.outcome = 'failure'
              AND se.started_at > NOW() - INTERVAL '7 days'
            ORDER BY se.started_at DESC
            LIMIT 5
        """))
        rows = rows_result.mappings().all()
        for row in rows:
            result["guardrails"].append({
                "skill": row["name"],
                "failure": (row["error_analysis"] or row["outcome_details"] or "Unknown")[:200],
                "when": str(row["started_at"]),
            })

        if skill:
            from brain.systems.memory.embedding_service import EmbeddingService

            embedding_service = await EmbeddingService.from_session(uow.session)
            skill_emb = embedding_service.query(skill)
            result["warnings"].extend(
                await maybe_await(uow.memories.high_salience_warnings_for_skill(skill_embedding=skill_emb))
            )

        if skill:
            from brain.platform.db.models.skill import Skill as SkillModel
            from sqlalchemy import select, or_

            stmt = select(SkillModel.pitfalls).where(
                SkillModel.name == skill,
                or_(SkillModel.archived == False, SkillModel.archived.is_(None)),  # noqa: E712
            )
            row_result = await session_execute(uow.session, stmt)
            row = row_result.scalar()
            if row:
                pitfalls = row if isinstance(row, list) else json.loads(row)
                result["pitfalls"] = [
                    {"text": pitfall["text"][:200], "severity": pitfall.get("severity", "medium")}
                    for pitfall in pitfalls[-5:]
                ]

    return result
