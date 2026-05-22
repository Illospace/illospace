"""Skill planning MCP tool implementation."""
from __future__ import annotations

import json
from typing import Any

from brain.app.mcp.tools.skill_common import SKILL_VIEW_SECTIONS
from brain.app.mcp.tools.skill_queries import (
    fetch_asset_paths_by_version,
    fetch_matching_skills,
    fetch_skill_card,
    fetch_skill_count,
)


def _coerce_triggers(value: Any) -> list:
    if not value:
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
    except Exception:
        return []
    return parsed if isinstance(parsed, list) else []


def _trigger_match(value: str, triggers: list) -> tuple[float, str | None]:
    lowered = (value or "").lower()
    for trigger in triggers:
        if isinstance(trigger, str):
            direction = "for"
            pattern = trigger
        elif isinstance(trigger, dict):
            direction = str(trigger.get("direction") or "for")
            pattern = str(trigger.get("pattern") or "")
        else:
            continue
        pattern = pattern.strip()
        if not pattern or direction in {"not_for", "against", "negative"}:
            continue
        pattern_lower = pattern.lower()
        if pattern_lower in lowered:
            return 1.0, pattern
        words = [part for part in pattern_lower.replace("-", " ").split() if len(part) > 2]
        if len(words) >= 2 and all(word in lowered for word in words):
            return 0.92, pattern
    return 0.0, None


def _looks_like_workspace_app_task(value: str) -> bool:
    lowered = (value or "").lower()
    markers = (
        "workspace app",
        "generated app",
        "build me an app",
        "build an app",
        "dashboard",
        "tracker",
        "todo list",
        "to-do list",
        "table",
        "graph",
        "chart",
        "crm",
        "review board",
        "widget",
    )
    action_markers = ("create", "build", "make", "generate", "save", "persistent")
    return any(marker in lowered for marker in markers) and any(
        marker in lowered for marker in action_markers
    )


def _looks_like_domain_task(value: str) -> bool:
    lowered = (value or "").lower()
    markers = (
        "domain",
        "domains",
        "manage_domain",
        "crm",
        "customer relationship",
        "outreach",
        "pipeline",
        "contacts",
        "companies",
        "records",
        "database",
        "structured data",
        "tracker",
        "track",
        "checklist",
        "todo",
        "to-do",
        "task list",
        "table",
        "list",
        "activity log",
        "operational log",
        "data log",
        "generated app",
        "workspace app",
    )
    action_markers = (
        "create",
        "build",
        "make",
        "set up",
        "setup",
        "manage",
        "maintain",
        "add",
        "record",
        "query",
        "using",
        "use",
        "see",
    )
    return any(marker in lowered for marker in markers) and any(
        marker in lowered for marker in action_markers
    )


async def brain_skills_tool(
    task: str,
    *,
    unit_of_work_cls: Any,
    maybe_await: Any,
    session_execute: Any,
    logger: Any,
) -> dict:
    """Task planning — recommend skills, guardrails, and strategy for a task."""
    from brain.systems.skills.builtin import ensure_builtin_skills_cached
    from brain.systems.memory.embedding_service import EmbeddingService, embedding_degradation_reason
    from brain.systems.memory.embeddings import vec_to_pg

    await ensure_builtin_skills_cached()
    small_skillset_threshold = 30

    async with unit_of_work_cls() as uow:
        task_emb = None
        emb_str = None
        embedding_error = None
        try:
            embedding_service = await EmbeddingService.from_session(uow.session)
            task_emb = embedding_service.query(task)
            emb_str = vec_to_pg(task_emb)
        except Exception as exc:
            embedding_error = embedding_degradation_reason(exc)[:300]
            logger.warning("brain_skills running in degraded mode; embedding unavailable: %s", embedding_error)

        skill_count = await fetch_skill_count(uow, session_execute=session_execute)
        matching_skills = await fetch_matching_skills(
            uow,
            emb_str=emb_str,
            skill_count=skill_count,
            small_skillset_threshold=small_skillset_threshold,
            session_execute=session_execute,
        )

        guardrail_memories = []
        if task_emb is not None:
            guardrail_memories = await maybe_await(uow.memories.guardrail_memories_for_task(
                task_embedding=task_emb,
                limit=5,
            ))

        matching_skills = await _promote_heuristic_skills(
            task,
            matching_skills,
            uow=uow,
            session_execute=session_execute,
        )
        asset_paths_by_version = await fetch_asset_paths_by_version(
            uow,
            matching_skills,
            maybe_await=maybe_await,
        )

    ranked = _rank_recommendations(
        task,
        task_emb=task_emb,
        skill_count=skill_count,
        small_skillset_threshold=small_skillset_threshold,
        matching_skills=matching_skills,
        asset_paths_by_version=asset_paths_by_version,
    )
    recommended = [item["card"] for item in ranked]
    result = {
        "task": task,
        "strategy": _strategy_for_ranked_recommendations(ranked),
        "recommended_skills": recommended,
        "guardrails": [memory["content"][:200] for memory in guardrail_memories],
    }
    if embedding_error:
        result["degraded"] = True
        result["degraded_reason"] = (
            "Embedding backend unavailable; returned a non-semantic skill catalog fallback. "
            f"Original error: {embedding_error}"
        )
    if not recommended:
        result["skill_gap"] = True
        result["skill_gap_hint"] = "No installed skills matched; proceed without skill-specific guidance."
    return result


async def _promote_heuristic_skills(
    task: str,
    matching_skills: list[Any],
    *,
    uow: Any,
    session_execute: Any,
) -> list[Any]:
    if _looks_like_workspace_app_task(task):
        matching_skills = await _promote_named_skill(
            "build-workspace-app",
            matching_skills,
            uow=uow,
            session_execute=session_execute,
        )
    if _looks_like_domain_task(task):
        matching_skills = await _promote_named_skill(
            "manage-domains",
            matching_skills,
            uow=uow,
            session_execute=session_execute,
        )
    return matching_skills


async def _promote_named_skill(
    name: str,
    matching_skills: list[Any],
    *,
    uow: Any,
    session_execute: Any,
) -> list[Any]:
    skill_card = await fetch_skill_card(uow, name, session_execute=session_execute)
    if skill_card is None:
        return matching_skills
    return [
        skill_card,
        *[skill for skill in matching_skills if str(skill.get("name")) != name],
    ]


def _rank_recommendations(
    task: str,
    *,
    task_emb: Any,
    skill_count: int,
    small_skillset_threshold: int,
    matching_skills: list[Any],
    asset_paths_by_version: dict[int, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    for skill in matching_skills:
        sim = float(skill["skill_match"]) if skill["skill_match"] else 0
        if task_emb is not None and skill_count > small_skillset_threshold and sim <= 0.3:
            continue
        card, composite = _recommendation_card(skill, task=task, task_emb=task_emb, similarity=sim)
        bundle_version_id = skill.get("bundle_version_id")
        if bundle_version_id:
            available_assets = asset_paths_by_version.get(int(bundle_version_id), [])
            if available_assets:
                card["assets"] = available_assets[:20]
                card["load_tools"]["assets"]["available_paths"] = [
                    asset["path"] for asset in available_assets[:20]
                ]
        ranked.append({
            "card": card,
            "sort_score": composite,
            "maturity": skill["maturity"] or "emerging",
            "success_rate": float(skill["success_rate"]) if skill["success_rate"] else 0,
        })
    ranked.sort(key=lambda item: item["sort_score"], reverse=True)
    return ranked


def _recommendation_card(skill: Any, *, task: str, task_emb: Any, similarity: float) -> tuple[dict, float]:
    success_rate = float(skill["success_rate"]) if skill["success_rate"] else 0
    centroid_match = skill.get("centroid_match")
    centroid_count = int(skill.get("centroid_count") or 0)
    if centroid_match is not None and centroid_count >= 3:
        semantic_score = float(centroid_match) * 0.7 + similarity * 0.3
    else:
        semantic_score = similarity

    if task_emb is None and similarity >= 1.0:
        composite = 1.1
    elif task_emb is None:
        composite = (
            float(skill["confidence"] or 0) * 0.6
            + success_rate * 0.3
            + min(int(skill["use_count"] or 0), 20) / 100
        )
    else:
        composite = semantic_score

    trigger_score, _matched_trigger = _trigger_match(task, _coerce_triggers(skill.get("triggers")))
    if trigger_score:
        composite = max(composite, trigger_score)
    return _skill_card_payload(skill), composite


def _skill_card_payload(skill: Any) -> dict:
    return {
        "name": skill["name"],
        "description": skill["description"] or "",
        "loaded_sections": ["catalog"],
        "available_sections": list(SKILL_VIEW_SECTIONS),
        "load_tools": {
            "card": {"tool": "skill_view", "arguments": {"name": skill["name"], "section": "card"}},
            "summary": {"tool": "skill_view", "arguments": {"name": skill["name"], "section": "summary"}},
            "procedure": {"tool": "skill_view", "arguments": {"name": skill["name"], "section": "procedure"}},
            "assets": {"tool": "skill_asset", "arguments": {"name": skill["name"], "path": "examples/..."}},
        },
    }


def _strategy_for_ranked_recommendations(ranked: list[dict[str, Any]]) -> str:
    if not ranked:
        return "full_pipeline"
    top = ranked[0]
    if top["maturity"] == "expert" and top["success_rate"] > 0.85:
        return "direct"
    if top["maturity"] in ("emerging", "developing"):
        return "investigate_first"
    return "full_pipeline"
