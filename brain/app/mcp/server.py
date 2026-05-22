#!/usr/bin/env python3
"""
MCP Brain Server — Exposes brain services as MCP tools.

Instead of pre-loading 5K tokens of context into every agent prompt,
this server lets agents pull context on demand via tool calls.
Only the information the agent actually needs gets loaded.

Usage:
    python3 mcp_brain_server.py                    # stdio transport (for MCP clients)
    python3 mcp_brain_server.py --http --port 9877  # HTTP transport (for testing)

MCP Tools Exposed:
    brain_recall(query, limit?)       — semantic memory search
    brain_guardrails(skill?)          — skill-specific guardrails + recent failures
    brain_skills(task)                — task planning + skill catalog recommendation
    skill_view(name, section?)        — load a skill card/summary/procedure section
    skill_asset(name, path)           — load a versioned skill bundle asset
    brain_encode(content, type, salience?) — record a memory
    vault_inventory()                 — list safe vault metadata for agent reasoning
    brain_vault(key)                  — retrieve a secret from the vault
    vault_secret_prompt(key_name)     — open a guided vault prompt for missing keys

Public release note: internal issue links were removed from source comments.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import urlencode

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))

from sqlalchemy import text

from brain.systems.memory.attention_controller import AttentionController, observe_retrieval
from brain.platform.db.repositories.memories import MemoryRepository
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.platform.db.repositories.memory_write_context import MemoryWriteContext
from brain.platform.db.repositories.memory_visibility import MemoryVisibilityContext
from brain.platform.providers.model_policy import DEFAULT_MODEL_TIER, normalize_model_tier

logger = logging.getLogger("mcp_brain")

async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _session_execute(session: Any, *args: Any, **kwargs: Any) -> Any:
    return await _maybe_await(session.execute(*args, **kwargs))


async def _session_flush(session: Any) -> None:
    await _maybe_await(session.flush())


def _jsonish(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value


def _json_safe(value: Any) -> Any:
    def convert(item: Any) -> str:
        if hasattr(item, "isoformat"):
            return item.isoformat()
        return str(item)

    return json.loads(json.dumps(value, default=convert))


def _truncate_text(value: str, max_chars: int) -> tuple[str, bool]:
    if max_chars <= 0:
        max_chars = 12000
    if len(value) <= max_chars:
        return value, False
    return value[:max_chars], True


def _skill_digest_metadata(skill: Any) -> dict[str, Any]:
    return {
        "skill_version": getattr(skill, "version", None),
        "bundle_version_id": getattr(skill, "bundle_version_id", None),
        "bundle_digest": getattr(skill, "bundle_digest", None),
        "overlay_revision": getattr(skill, "overlay_revision", None),
        "effective_digest": getattr(skill, "effective_digest", None)
        or getattr(skill, "bundle_digest", None),
        "source_kind": getattr(skill, "source_kind", None) or "legacy_db",
        "trust_level": getattr(skill, "trust_level", None) or "private_local",
    }


SKILL_VIEW_SECTIONS = (
    "card",
    "summary",
    "procedure",
    "pitfalls",
    "triggers",
    "guardrails",
    "graduated_steps",
    "metadata",
)


def _skill_card(skill: Any) -> dict[str, str]:
    return {
        "name": getattr(skill, "name", "") or "",
        "description": getattr(skill, "description", "") or "",
    }


def _compact_skill_item(item: Any) -> str:
    if isinstance(item, dict):
        for key in ("text", "pattern", "action", "condition", "summary", "description"):
            value = item.get(key)
            if value:
                return str(value)
        return json.dumps(item, ensure_ascii=False, default=str)
    return str(item)


def _compact_skill_items(value: Any, *, limit: int = 3, max_chars: int = 180) -> list[str]:
    items = _jsonish(value, [])
    if not isinstance(items, list):
        return []
    compact: list[str] = []
    for item in items[:limit]:
        text_value, _ = _truncate_text(_compact_skill_item(item).strip(), max_chars)
        if text_value:
            compact.append(text_value)
    return compact


def _skill_summary(skill: Any, max_chars: int) -> tuple[str, bool]:
    parts: list[str] = []
    description = str(getattr(skill, "description", "") or "").strip()
    if description:
        parts.append(f"Description: {description}")

    procedure = str(getattr(skill, "procedure", "") or "").strip()
    if procedure:
        preview, preview_truncated = _truncate_text(procedure, 900)
        label = "Procedure preview"
        if preview_truncated:
            label += " (truncated)"
        parts.append(f"{label}:\n{preview}")

    for label, attr in (
        ("Triggers", "triggers"),
        ("Guardrails", "guardrails"),
        ("Pitfalls", "pitfalls"),
        ("Graduated steps", "graduated_steps"),
    ):
        items = _compact_skill_items(getattr(skill, attr, None))
        if items:
            parts.append(label + ":\n" + "\n".join(f"- {item}" for item in items))

    if not parts:
        parts.append(f"{getattr(skill, 'name', '') or 'Skill'}: no summary content is available.")

    return _truncate_text("\n\n".join(parts), max_chars)


def _validate_skill_asset_path(path: str) -> str:
    cleaned = (path or "").strip()
    if not cleaned:
        raise ValueError("path is required")
    if "\\" in cleaned:
        raise ValueError("path must use POSIX separators")
    if PurePosixPath(cleaned).is_absolute():
        raise ValueError("path must be relative")
    windows_path = PureWindowsPath(cleaned)
    if windows_path.is_absolute() or windows_path.drive:
        raise ValueError("path must be relative")
    parts = cleaned.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("path cannot contain traversal segments")
    return cleaned


async def _add_attribution(session, memories: list[dict], current_user_id: str) -> list[dict]:
    """Add attribution info to shared memories from other users.

    If the source user has attribution_enabled=True, shows their name.
    Otherwise, shows "A teammate" (anonymous).
    """
    try:
        return await MemoryRepository(session).add_attribution(memories, current_user_id)
    except Exception:
        pass
    return memories


async def _async_log_retrieval(query: str, results: list) -> None:
    """Insert a row into retrieval_log for metrics tracking. Non-blocking."""
    try:
        top_score = max((r.get("similarity", 0) for r in results), default=0)
        async with UnitOfWork() as uow:
            await _maybe_await(uow.retrieval_logs.create(
                query_text=query[:500],
                results_returned=len(results),
                top_score=round(float(top_score), 4),
            ))
            await _session_flush(uow.session)
    except Exception as e:
        logger.debug(f"retrieval_log insert failed (non-critical): {e}")


async def async_tool_brain_recall(
    query: str,
    limit: int = 3,
    user_id: str | None = None,
    org_id: str | None = None,
    attention_debug: bool = False,
    expand_lazy_load: bool | None = None,
    service_retrieval: bool = False,
) -> dict:
    """Graph-augmented memory search — vector similarity + relationship traversal.

    Multiplayer: pass user_id + org_id for visibility-scoped recall.
    Without viewer context, recall intentionally returns no memories.
    """
    from brain.systems.memory.embeddings import embed_query
    from brain.systems.runtime_settings.memory import async_get_embedding_runtime_config

    visibility_context = MemoryVisibilityContext(
        user_id=user_id,
        org_id=org_id,
        allow_global=service_retrieval or (user_id == "system"),
        principal_type="service" if service_retrieval or user_id == "system" else None,
    )

    try:
        async with UnitOfWork() as uow:
            runtime_config = await async_get_embedding_runtime_config(uow.session, include_secret=True)
            query_emb = embed_query(query, runtime_config=runtime_config)
            results = await _maybe_await(uow.memories.graph_augmented_recall(
                query_embedding=query_emb,
                limit=limit,
                context=visibility_context,
            ))
            memories = []
            for r in results:
                mem = {
                    "id": r["id"],
                    "content": r["content"][:300],
                    "type": r["type"],
                    "tier": r.get("tier", "episodic"),
                    "salience": r.get("salience", 0),
                    "similarity": r.get("similarity", 0),
                    "visibility": r.get("visibility", "private"),
                }
                if r.get("graph_edges"):
                    mem["graph_context"] = r["graph_edges"][:3]
                memories.append(mem)
            # Add cross-user attribution for shared memories
            if user_id and memories:
                memories = await _maybe_await(uow.memories.add_attribution(memories, user_id))
        return await _finalize_recall_response(
            query=query,
            memories=memories,
            limit=limit,
            user_id=user_id,
            org_id=org_id,
            attention_debug=attention_debug,
            expand_lazy_load=expand_lazy_load,
            service_retrieval=service_retrieval,
        )
    except Exception as e:
        logger.warning(f"Graph recall failed, falling back to vector: {e}")

    # Fallback: pure vector search with same visibility filtering
    async with UnitOfWork() as uow:
        memories = await _maybe_await(uow.memories.recall_vector(
            query_embedding=query_emb,
            limit=limit,
            context=visibility_context,
        ))
        if user_id and memories:
            memories = await _maybe_await(uow.memories.add_attribution(memories, user_id))

    return await _finalize_recall_response(
        query=query,
        memories=memories,
        limit=limit,
        user_id=user_id,
        org_id=org_id,
        attention_debug=attention_debug,
        expand_lazy_load=expand_lazy_load,
        service_retrieval=service_retrieval,
    )


async def _finalize_recall_response(
    *,
    query: str,
    memories: list[dict],
    limit: int,
    user_id: str | None,
    org_id: str | None,
    attention_debug: bool,
    expand_lazy_load: bool | None,
    service_retrieval: bool = False,
) -> dict:

    await _async_log_retrieval(query, memories)
    service_retrieval = service_retrieval or user_id == "system"
    if not user_id and not org_id and not service_retrieval and not memories:
        return {
            "memories": [],
            "candidate_memories": [],
            "suppressed_memories": [],
            "lazy_load_memories": [],
            "lazy_loaded_memories": [],
            "count": 0,
            "candidate_count": 0,
            "attention_decision": {
                "stage": "brain_recall",
                "retrieval_decision_id": None,
                "selected_count": 0,
                "candidate_count": 0,
                "service_retrieval": False,
                "fallback_used": True,
                "fallback_reason": "missing_user_context",
            },
        }
    attention_decision = await observe_retrieval(
        stage="brain_recall",
        query_text=query,
        candidates=memories,
        user_id=user_id,
        org_id=org_id,
        service_retrieval=service_retrieval,
        preload_budget_tokens=limit * 120,
        lazy_budget_tokens=max(0, limit * 40),
    )
    selection = AttentionController().materialize_selection(memories, attention_decision)
    lazy_loaded_memories: list[dict] = []
    retrieval_decision_id = attention_decision.get("retrieval_decision_id")
    should_expand = (
        expand_lazy_load if expand_lazy_load is not None
        else os.getenv("ATTENTION_LAZY_LOAD_ENABLED", "0").strip().lower() not in {"0", "false", "no"}
    )
    if should_expand and selection.lazy_load_eligible and retrieval_decision_id is not None:
        lazy_loaded_memories = await AttentionController().load_lazy_candidates(
            retrieval_decision_id=int(retrieval_decision_id),
            user_id=user_id,
            org_id=org_id,
            service_retrieval=service_retrieval,
            limit=max(0, limit - len(selection.selected)),
        )
    visible_memories = list(selection.selected) + list(lazy_loaded_memories)
    return {
        "memories": visible_memories,
        "candidate_memories": memories,
        "suppressed_memories": selection.suppressed,
        "lazy_load_memories": selection.lazy_load_eligible,
        "lazy_loaded_memories": lazy_loaded_memories,
        "count": len(visible_memories),
        "candidate_count": len(memories),
        "attention_decision": attention_decision,
        **({"attention_explain": AttentionController().explain(attention_decision, memories)} if attention_debug else {}),
    }


async def async_tool_brain_guardrails(skill: str | None = None) -> dict:
    """Get guardrails: recent failures, high-salience warnings, and skill-specific pitfalls."""
    result = {"guardrails": [], "warnings": [], "pitfalls": []}

    async with UnitOfWork() as uow:
        # Recent failures (last 7 days)
        rows_result = await _session_execute(uow.session, text("""
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

        # High-salience warnings (lessons with salience >= 9)
        if skill:
            from brain.systems.memory.embeddings import embed_query
            from brain.systems.runtime_settings.memory import async_get_embedding_runtime_config

            runtime_config = await async_get_embedding_runtime_config(uow.session, include_secret=True)
            skill_emb = embed_query(skill, runtime_config=runtime_config)
            result["warnings"].extend(
                await _maybe_await(uow.memories.high_salience_warnings_for_skill(skill_embedding=skill_emb))
            )

        # Skill-specific pitfalls
        if skill:
            from brain.platform.db.models.skill import Skill as SkillModel
            from sqlalchemy import select, or_
            stmt = select(SkillModel.pitfalls).where(
                SkillModel.name == skill,
                or_(SkillModel.archived == False, SkillModel.archived.is_(None)),  # noqa: E712
            )
            row_result = await _session_execute(uow.session, stmt)
            row = row_result.scalar()
            if row:
                pitfalls = row if isinstance(row, list) else json.loads(row)
                result["pitfalls"] = [
                    {"text": p["text"][:200], "severity": p.get("severity", "medium")}
                    for p in pitfalls[-5:]  # latest 5
                ]

    return result


async def async_tool_brain_skills(task: str) -> dict:
    """Task planning — recommend skills, guardrails, and strategy for a task.

    Returns the same structure as `skills.py plan` but without the subprocess overhead.
    """
    from brain.systems.skills.builtin import ensure_builtin_skills_cached
    from brain.systems.memory.embeddings import embed_query, vec_to_pg

    await ensure_builtin_skills_cached()

    def _coerce_triggers(value) -> list:
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

    async def _fetch_skill_card(uow, name: str):
        result = await _session_execute(uow.session, text("""
            SELECT id, name, description, version, maturity, confidence, use_count,
                   success_count::float / GREATEST(use_count, 1) as success_rate,
                   model_tier, thinking_tier, pitfalls, triggers,
                   bundle_version_id, bundle_digest, overlay_revision,
                   effective_digest, source_kind, trust_level,
                   1.0 as skill_match,
                   NULL as centroid_match,
                   centroid_count
            FROM skills
            WHERE NOT archived
              AND skill_type != 'meta'
              AND name = :name
            LIMIT 1
        """), {"name": name})
        return result.mappings().first()

    _SMALL_SKILLSET_THRESHOLD = 30  # below this, send ALL skills — model picks better than embeddings

    async with UnitOfWork() as uow:
        task_emb = None
        emb_str = None
        embedding_error = None
        try:
            from brain.systems.runtime_settings.memory import async_get_embedding_runtime_config

            runtime_config = await async_get_embedding_runtime_config(uow.session, include_secret=True)
            task_emb = embed_query(task, runtime_config=runtime_config)
            emb_str = vec_to_pg(task_emb)
        except Exception as exc:
            embedding_error = str(exc)[:300]
            logger.warning("brain_skills running in degraded mode; embedding unavailable: %s", embedding_error)

        # Count active skills to decide strategy
        count_result = await _session_execute(uow.session, text(
            "SELECT COUNT(*) as cnt FROM skills WHERE NOT archived"
        ))
        count_row = count_result.mappings().one()
        skill_count = count_row["cnt"]

        if emb_str is None:
            limit_clause = "" if skill_count <= _SMALL_SKILLSET_THRESHOLD else "LIMIT 15"
            matching_result = await _session_execute(uow.session, text(f"""
                SELECT id, name, description, version, maturity, confidence, use_count,
                       success_count::float / GREATEST(use_count, 1) as success_rate,
                       model_tier, thinking_tier, pitfalls, triggers,
                       bundle_version_id, bundle_digest, overlay_revision,
                       effective_digest, source_kind, trust_level,
                       0.0 as skill_match,
                       NULL as centroid_match,
                       centroid_count
                FROM skills
                WHERE NOT archived AND skill_type != 'meta'
                ORDER BY use_count DESC, confidence DESC, name ASC
                {limit_clause}
            """))
            matching_skills = matching_result.mappings().all()
        elif skill_count <= _SMALL_SKILLSET_THRESHOLD:
            matching_result = await _session_execute(uow.session, text("""
                SELECT id, name, description, version, maturity, confidence, use_count,
                       success_count::float / GREATEST(use_count, 1) as success_rate,
                       model_tier, thinking_tier, pitfalls, triggers,
                       bundle_version_id, bundle_digest, overlay_revision,
                       effective_digest, source_kind, trust_level,
                       1 - (embedding <=> CAST(:emb1 AS vector)) as skill_match,
                       CASE WHEN task_centroid IS NOT NULL
                            THEN 1 - (task_centroid <=> CAST(:emb2 AS vector)) ELSE NULL
                       END as centroid_match,
                       centroid_count
                FROM skills
                WHERE NOT archived AND skill_type != 'meta'
                ORDER BY
                    CASE WHEN centroid_count >= 3
                         THEN (1 - (task_centroid <=> CAST(:emb3 AS vector))) * 0.7 + (1 - (embedding <=> CAST(:emb4 AS vector))) * 0.3
                         ELSE 1 - (embedding <=> CAST(:emb5 AS vector))
                    END DESC
            """), {"emb1": emb_str, "emb2": emb_str, "emb3": emb_str, "emb4": emb_str, "emb5": emb_str})
            matching_skills = matching_result.mappings().all()
        else:
            matching_result = await _session_execute(uow.session, text("""
                SELECT id, name, description, version, maturity, confidence, use_count,
                       success_count::float / GREATEST(use_count, 1) as success_rate,
                       model_tier, thinking_tier, pitfalls, triggers,
                       bundle_version_id, bundle_digest, overlay_revision,
                       effective_digest, source_kind, trust_level,
                       CASE WHEN embedding IS NOT NULL
                            THEN 1 - (embedding <=> CAST(:emb1 AS vector))
                            ELSE 0.35
                       END as skill_match,
                       CASE WHEN task_centroid IS NOT NULL
                            THEN 1 - (task_centroid <=> CAST(:emb2 AS vector)) ELSE NULL
                       END as centroid_match,
                       centroid_count
                FROM skills
                WHERE NOT archived AND skill_type != 'meta' AND (embedding IS NOT NULL OR builtin IS TRUE)
                ORDER BY
                    CASE WHEN embedding IS NULL
                         THEN 0.35
                         WHEN centroid_count >= 3
                         THEN (1 - (task_centroid <=> CAST(:emb3 AS vector))) * 0.7 + (1 - (embedding <=> CAST(:emb4 AS vector))) * 0.3
                         ELSE 1 - (embedding <=> CAST(:emb5 AS vector))
                    END DESC
                LIMIT 15
            """), {"emb1": emb_str, "emb2": emb_str, "emb3": emb_str, "emb4": emb_str, "emb5": emb_str})
            matching_skills = matching_result.mappings().all()

        # Relevant memories (lessons/patterns for guardrails)
        if task_emb is None:
            guardrail_memories = []
        else:
            guardrail_memories = await _maybe_await(uow.memories.guardrail_memories_for_task(
                task_embedding=task_emb,
                limit=5,
            ))

        if _looks_like_workspace_app_task(task):
            workspace_app_skill = await _fetch_skill_card(uow, "build-workspace-app")
            if workspace_app_skill is not None:
                matching_skills = [
                    workspace_app_skill,
                    *[
                        skill
                        for skill in matching_skills
                        if str(skill.get("name")) != "build-workspace-app"
                    ],
                ]
        if _looks_like_domain_task(task):
            domain_skill = await _fetch_skill_card(uow, "manage-domains")
            if domain_skill is not None:
                matching_skills = [
                    domain_skill,
                    *[
                        skill
                        for skill in matching_skills
                        if str(skill.get("name")) != "manage-domains"
                    ],
                ]

        asset_paths_by_version: dict[int, list[dict[str, Any]]] = {}
        bundle_version_ids = {
            int(skill["bundle_version_id"])
            for skill in matching_skills
            if skill.get("bundle_version_id")
        }
        for version_id in bundle_version_ids:
            try:
                assets = await _maybe_await(uow.skill_bundles.list_assets(version_id))
            except Exception:
                continue
            asset_paths_by_version[version_id] = [
                {
                    "path": asset.path,
                    "kind": asset.asset_kind,
                    "mime_type": asset.mime_type,
                }
                for asset in assets
            ]

    # Rank skills by semantic match — find the right skill for the task.
    # Quality/maturity can still inform degraded-mode ranking and strategy,
    # but the public skill card stays small: name, description, and load handles.
    # If a skill is bad, fix the skill — don't route around it.
    ranked_recommendations: list[dict[str, Any]] = []
    for s in matching_skills:
        sim = float(s["skill_match"]) if s["skill_match"] else 0
        if task_emb is not None and skill_count > _SMALL_SKILLSET_THRESHOLD and sim <= 0.3:
            continue

        success_rate = float(s["success_rate"]) if s["success_rate"] else 0
        use_count = int(s["use_count"] or 0)
        maturity = s["maturity"] or "emerging"

        # Use centroid match when available (learned from actual task routing),
        # fall back to description embedding match.
        centroid_match = s.get("centroid_match")
        centroid_count = int(s.get("centroid_count") or 0)
        if centroid_match is not None and centroid_count >= 3:
            semantic_score = float(centroid_match) * 0.7 + sim * 0.3
        else:
            semantic_score = sim

        # Skill selection is purely semantic: find the skill that matches
        # the task. There should be one right skill per task type. If that
        # skill has low quality, the fix is to improve the skill — not to
        # route around it by picking a generic one.
        if task_emb is None and sim >= 1.0:
            composite = 1.1
        elif task_emb is None:
            composite = (
                float(s["confidence"] or 0) * 0.6
                + success_rate * 0.3
                + min(use_count, 20) / 100
            )
        else:
            composite = semantic_score

        skill_card = {
            "name": s["name"],
            "description": s["description"] or "",
            "loaded_sections": ["catalog"],
            "available_sections": list(SKILL_VIEW_SECTIONS),
            "load_tools": {
                "card": {
                    "tool": "skill_view",
                    "arguments": {"name": s["name"], "section": "card"},
                },
                "summary": {
                    "tool": "skill_view",
                    "arguments": {"name": s["name"], "section": "summary"},
                },
                "procedure": {
                    "tool": "skill_view",
                    "arguments": {"name": s["name"], "section": "procedure"},
                },
                "assets": {
                    "tool": "skill_asset",
                    "arguments": {"name": s["name"], "path": "examples/..."},
                },
            },
        }
        triggers = _coerce_triggers(s.get("triggers"))
        trigger_score, matched_trigger = _trigger_match(task, triggers)
        if trigger_score:
            composite = max(composite, trigger_score)
        bundle_version_id = s.get("bundle_version_id")
        if bundle_version_id:
            available_assets = asset_paths_by_version.get(int(bundle_version_id), [])
            if available_assets:
                skill_card["assets"] = available_assets[:20]
                skill_card["load_tools"]["assets"]["available_paths"] = [
                    asset["path"] for asset in available_assets[:20]
                ]
        ranked_recommendations.append({
            "card": skill_card,
            "sort_score": composite,
            "maturity": maturity,
            "success_rate": success_rate,
            "matched_trigger": matched_trigger,
        })

    # Sort by composite score — best overall skill first, not just best keyword match
    ranked_recommendations.sort(key=lambda item: item["sort_score"], reverse=True)
    recommended = [item["card"] for item in ranked_recommendations]

    guardrails = [m["content"][:200] for m in guardrail_memories]

    # Determine strategy based on top skill (now composite-best, not just embedding-best)
    strategy = "full_pipeline"
    if ranked_recommendations:
        top = ranked_recommendations[0]
        if top["maturity"] == "expert" and top["success_rate"] > 0.85:
            strategy = "direct"
        elif top["maturity"] in ("emerging", "developing"):
            strategy = "investigate_first"

    # Skill gap: only when no skills match at all. Skill authoring is currently
    # kept out of the live model tool surface, so this is informational only.
    skill_gap = len(recommended) == 0

    result = {
        "task": task,
        "strategy": strategy,
        "recommended_skills": recommended,
        "guardrails": guardrails,
    }
    if embedding_error:
        result["degraded"] = True
        result["degraded_reason"] = (
            "Embedding backend unavailable; returned a non-semantic skill catalog fallback. "
            f"Original error: {embedding_error}"
        )
    if skill_gap:
        result["skill_gap"] = True
        result["skill_gap_hint"] = "No installed skills matched; proceed without skill-specific guidance."
    return result


async def async_tool_skill_view(
    name: str,
    section: str = "procedure",
    max_chars: int = 12000,
) -> dict:
    """Load a specific skill section on demand."""
    section = (section or "procedure").strip().lower()
    allowed = set(SKILL_VIEW_SECTIONS)
    if section not in allowed:
        return {
            "error": f"Unknown skill section: {section}",
            "allowed_sections": sorted(allowed),
        }

    async with UnitOfWork() as uow:
        skill = await _maybe_await(uow.skills.get_by_name(name))
        if skill is None:
            return {"error": f"Skill '{name}' not found"}

        if section == "card":
            return {
                **_skill_card(skill),
                "section": section,
                "loaded_sections": [section],
            }

        metadata = _skill_digest_metadata(skill)
        payload: dict[str, Any] = {
            "name": skill.name,
            "section": section,
            "loaded_sections": [section],
            **metadata,
        }

        if section == "summary":
            text_value, truncated = _skill_summary(skill, max_chars)
            payload.update({
                "description": skill.description or "",
                "content_type": "text/markdown",
                "content": text_value,
                "truncated": truncated,
            })
        elif section == "procedure":
            text_value, truncated = _truncate_text(skill.procedure or "", max_chars)
            payload.update({
                "content_type": "text/markdown",
                "content": text_value,
                "truncated": truncated,
            })
        elif section == "pitfalls":
            payload["items"] = _jsonish(skill.pitfalls, [])
        elif section == "triggers":
            payload["items"] = _jsonish(skill.triggers, [])
        elif section == "guardrails":
            payload["items"] = _jsonish(skill.guardrails, [])
        elif section == "graduated_steps":
            payload["items"] = _jsonish(skill.graduated_steps, [])
        else:
            payload["metadata"] = {
                "description": skill.description or "",
                "maturity": skill.maturity or "emerging",
                "confidence": float(skill.confidence or 0),
                "use_count": int(skill.use_count or 0),
                "success_count": int(skill.success_count or 0),
                "failure_count": int(skill.failure_count or 0),
                "model_tier": normalize_model_tier(skill.model_tier) or DEFAULT_MODEL_TIER,
                "thinking_tier": skill.thinking_tier or "medium",
                "skill_type": skill.skill_type or "skill",
            }
        return payload


async def async_tool_skill_asset(
    name: str,
    path: str,
    max_chars: int = 12000,
) -> dict:
    """Load a specific asset from the installed skill bundle version."""
    try:
        safe_path = _validate_skill_asset_path(path)
    except ValueError as exc:
        return {"error": str(exc)}

    async with UnitOfWork() as uow:
        skill = await _maybe_await(uow.skills.get_by_name(name))
        if skill is None:
            return {"error": f"Skill '{name}' not found"}
        if not skill.bundle_version_id:
            return {
                "error": f"Skill '{name}' is not backed by a bundle version",
                "name": skill.name,
                **_skill_digest_metadata(skill),
            }

        assets = await _maybe_await(uow.skill_bundles.list_assets(skill.bundle_version_id))
        asset = next((item for item in assets if item.path == safe_path), None)
        if asset is None:
            return {
                "error": f"Skill asset not found: {safe_path}",
                "name": skill.name,
                "path": safe_path,
                "available_assets": [item.path for item in assets[:50]],
                **_skill_digest_metadata(skill),
            }

        content = asset.content_text
        truncated = False
        if content is not None:
            content, truncated = _truncate_text(content, max_chars)

        return {
            "name": skill.name,
            "path": asset.path,
            "asset_kind": asset.asset_kind,
            "mime_type": asset.mime_type,
            "size_bytes": asset.size_bytes,
            "content_digest": asset.content_digest,
            "storage_kind": asset.storage_kind,
            "storage_uri": asset.storage_uri,
            "content": content,
            "truncated": truncated,
            "loaded_sections": [f"asset:{asset.path}"],
            **_skill_digest_metadata(skill),
        }


async def async_tool_brain_encode(
    content: str,
    memory_type: str = "episode",
    salience: float = 5.0,
    source: str = "agent_run",
    user_id: str | None = None,
    org_id: str | None = None,
    visibility: str = "private",
    conversation_id: str | None = None,
    idea_id: str | None = None,
    run_id: int | str | None = None,
    session_id: str | None = None,
    confidence: float | None = None,
    evidence: dict | None = None,
) -> dict:
    """Encode a new memory into the brain, scoped to the current user."""
    from brain.systems.memory.embeddings import embed_document
    from brain.systems.runtime_settings.memory import async_get_embedding_runtime_config

    if len(content.strip()) < 20:
        return {"error": "Content too short (min 20 chars)"}

    if not user_id:
        return {"error": "brain_encode requires user context (missing user_id)"}

    if visibility not in ("private", "team", "org"):
        visibility = "private"

    try:
        write_context = MemoryWriteContext(
            user_id=user_id,
            org_id=org_id,
            visibility=visibility,
            source=source,
            conversation_id=conversation_id,
            idea_id=idea_id,
            run_id=run_id,
            session_id=session_id,
            confidence=confidence,
            evidence=evidence or {},
        )
    except ValueError as exc:
        return {"error": str(exc)}

    semantic_emb = None
    degraded_reason = None

    async with UnitOfWork() as uow:
        try:
            runtime_config = await async_get_embedding_runtime_config(uow.session, include_secret=True)
            semantic_emb = embed_document(content, runtime_config=runtime_config)
        except Exception as exc:
            error_text = str(exc)
            lower = error_text.lower()
            if any(term in lower for term in ("out of memory", "oom", "cuda")):
                degraded_reason = f"embedding_oom: {error_text[:200]}"
            else:
                degraded_reason = f"embedding_failed: {error_text[:200]}"

        result = await _maybe_await(uow.memories.insert_memory(
            content=content,
            memory_type=memory_type,
            salience=salience,
            semantic_embedding=semantic_emb,
            context=write_context,
            auto_edge=False,
        ))

    if degraded_reason:
        result["warning"] = degraded_reason
        result["embedding_deferred"] = True
    return result


async def tool_brain_vault(
    key: str,
    reason: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
    run_id: int | None = None,
    idea_id: str | None = None,
    requested_by: str = "agent",
    project_slug: str | None = None,
    project_slugs: list[str] | tuple[str, ...] | set[str] | None = None,
    target_registry_id: int | None = None,
) -> dict:
    """Retrieve a secret from the vault."""
    from brain.systems.cortex.events import publish_safe
    from brain.systems.vault import authorize_agent_secret_read, get_secret
    if not user_id:
        return {"error": "Vault access requires an authenticated user context"}
    target_user_id = str(user_id).strip()
    if not target_user_id:
        return {"error": "Vault access requires an authenticated user context"}
    authorization = await authorize_agent_secret_read(
        key,
        actor_user_id=target_user_id,
        org_id=org_id,
        run_id=run_id,
        reason=reason,
        requested_by=requested_by,
        project_slug=project_slug,
        project_slugs=project_slugs,
        target_registry_id=target_registry_id,
    )
    if not authorization.get("allowed"):
        grant = _json_safe(authorization.get("grant") or {})
        grant_user_id = str(grant.get("requested_by_user_id") or target_user_id).strip() or target_user_id
        if authorization.get("status") == "pending":
            normalized_idea_id = (str(idea_id).strip() if idea_id else "") or None
            prompt = None
            if normalized_idea_id:
                prompt = {
                    "id": f"vault-grant-{grant.get('id') or run_id or 'thread'}",
                    "idea_id": normalized_idea_id,
                    "org_id": org_id,
                    "target_user_id": grant_user_id,
                    "run_id": grant.get("run_id") or run_id,
                    "grant_id": grant.get("id"),
                    "key_name": grant.get("key_name") or key,
                    "requested_by": grant.get("requested_by") or requested_by,
                    "reason": grant.get("reason") or reason,
                    "requested_at": grant.get("requested_at"),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
                publish_safe("vault_agent_grant_prompt", {
                    "idea_id": normalized_idea_id,
                    "org_id": org_id,
                    "target_user_id": grant_user_id,
                    "run_id": grant.get("run_id") or run_id,
                    "grant": grant,
                    "prompt": prompt,
                })
            response = {
                "error": "Vault grant required before this agent can read the secret",
                "grant_id": grant.get("id"),
                "key_name": grant.get("key_name") or key,
                "reason": grant.get("reason") or reason,
                "requested_by": grant.get("requested_by") or requested_by,
                "run_id": grant.get("run_id") or run_id,
                "status": "pending",
                "target_user_id": grant_user_id,
            }
            if prompt:
                response["prompt"] = prompt
            return response
        return {"error": authorization.get("reason") or "Vault grant denied"}
    value = await get_secret(
        key,
        actor_user_id=target_user_id,
        org_id=org_id,
        accessed_by="agent",
    )
    if value is None:
        return {"error": f"Secret '{key}' not found in vault"}
    return {"key": key, "value": value}


_VAULT_PROMPT_CATEGORIES = {
    "general",
    "api",
    "aws",
    "auth",
    "analytics",
    "database",
    "messaging",
    "monitoring",
    "payments",
    "service",
}


def _clean_vault_prompt_text(value: str | None, *, max_chars: int = 240) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "..."


def _normalize_vault_key_name(key_name: str) -> str:
    cleaned = str(key_name or "").strip()
    if not cleaned:
        raise ValueError("key_name is required")
    return cleaned.upper()


def _safe_vault_secret_summary(secret: dict[str, Any]) -> dict[str, Any]:
    return {
        "key_name": str(secret.get("key_name") or ""),
        "description": str(secret.get("description") or ""),
        "category": str(secret.get("category") or "general"),
        "agent_access_level": str(secret.get("agent_access_level") or "ask"),
    }


def _vault_prompt_url(
    *,
    key_name: str,
    description: str,
    category: str,
) -> str:
    return "/vault?" + urlencode({
        "add_secret": key_name,
        "description": description,
        "category": category,
    })


async def tool_vault_inventory(
    category: str | None = None,
    access_level: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
) -> dict:
    """List safe Vault metadata so the agent can choose an existing key."""
    from brain.systems.vault import async_list_secrets

    if not user_id:
        return {"error": "Vault inventory requires an authenticated user context"}
    normalized_user_id = str(user_id).strip()
    if not normalized_user_id:
        return {"error": "Vault inventory requires an authenticated user context"}
    normalized_org_id = (str(org_id).strip() if org_id else "") or None
    normalized_category = str(category or "").strip().lower() or None
    normalized_access_level = str(access_level or "").strip().lower() or None

    secrets = [
        _safe_vault_secret_summary(secret)
        for secret in await async_list_secrets(
            actor_user_id=normalized_user_id,
            org_id=normalized_org_id,
            category=normalized_category,
        )
    ]
    if normalized_access_level:
        secrets = [
            secret
            for secret in secrets
            if secret["agent_access_level"].strip().lower() == normalized_access_level
        ]
    secrets.sort(key=lambda secret: (secret["category"], secret["key_name"]))
    return {
        "secrets": secrets,
        "count": len(secrets),
        "metadata_only": True,
        "guidance": (
            "Use these names/descriptions/categories to decide which exact key to request with brain_vault. "
            "If no suitable key exists, ask the user or call vault_secret_prompt."
        ),
    }


async def tool_vault_secret_prompt(
    key_name: str,
    description: str | None = None,
    category: str = "api",
    reason: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
    run_id: int | None = None,
    idea_id: str | None = None,
    requested_by: str = "agent",
) -> dict:
    """Open a guided Vault form for a user-supplied secret value."""
    from brain.systems.cortex.events import publish_safe
    from brain.systems.vault import record_missing_request

    if not user_id:
        return {"error": "Vault secret prompts require an authenticated user context"}

    normalized_user_id = str(user_id).strip()
    if not normalized_user_id:
        return {"error": "Vault secret prompts require an authenticated user context"}
    normalized_org_id = (str(org_id).strip() if org_id else "") or None
    normalized_idea_id = (str(idea_id).strip() if idea_id else "") or None

    try:
        normalized_key = _normalize_vault_key_name(key_name)
    except ValueError as exc:
        return {"error": str(exc)}

    normalized_category = str(category or "api").strip().lower() or "api"
    if normalized_category not in _VAULT_PROMPT_CATEGORIES:
        normalized_category = "general"
    clean_description = _clean_vault_prompt_text(
        description or f"Credential requested by Illo for {normalized_key}.",
    )
    clean_reason = _clean_vault_prompt_text(reason or clean_description, max_chars=360)
    clean_requested_by = _clean_vault_prompt_text(requested_by or "agent", max_chars=80) or "agent"

    prompt = {
        "id": f"vault-secret-{run_id or 'thread'}-{uuid.uuid4().hex[:10]}",
        "idea_id": normalized_idea_id,
        "org_id": normalized_org_id,
        "run_id": run_id,
        "key_name": normalized_key,
        "description": clean_description,
        "category": normalized_category,
        "reason": clean_reason,
        "requested_by": clean_requested_by,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    await record_missing_request(normalized_key, actor_user_id=normalized_user_id, org_id=normalized_org_id)

    if normalized_idea_id:
        publish_safe("vault_secret_prompt", {
            "idea_id": normalized_idea_id,
            "org_id": normalized_org_id,
            "run_id": run_id,
            "prompt": prompt,
            "key_name": normalized_key,
            "description": clean_description,
            "category": normalized_category,
            "reason": clean_reason,
            "requested_by": clean_requested_by,
        })

    response = {
        "prompted": bool(normalized_idea_id),
        "status": "opened" if normalized_idea_id else "recorded",
        "key_name": normalized_key,
        "description": clean_description,
        "category": normalized_category,
        "prompt": prompt,
        "vault_url": _vault_prompt_url(
            key_name=normalized_key,
            description=clean_description,
            category=normalized_category,
        ),
    }
    if not normalized_idea_id:
        response["warning"] = (
            "No current Cortex thread was bound, so the missing key was recorded for Vault."
        )
    return response


async def tool_runtime_settings(
    provider: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
) -> dict:
    """Inspect active runtime/provider/auth settings for the current user."""
    from brain.systems.services.runtime_introspection import async_get_runtime_settings_snapshot

    async with UnitOfWork() as uow:
        return await async_get_runtime_settings_snapshot(
            uow.session,
            user_id=user_id,
            org_id=org_id,
            provider=provider,
        )


# ── MCP Protocol Layer ───────────────────────────────────────

TOOLS = {
    "brain_recall": {
        "function": async_tool_brain_recall,
        "description": "Search brain memories semantically. Returns the most relevant memories for the query.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for in brain memories"},
                "limit": {"type": "integer", "description": "Max results (default 3)", "default": 3},
                "attention_debug": {"type": "boolean", "description": "Include controller debug breakdown", "default": False},
                "expand_lazy_load": {"type": "boolean", "description": "Fetch deferred lazy-load candidates", "default": False},
            },
            "required": ["query"],
        },
    },
    "brain_guardrails": {
        "function": async_tool_brain_guardrails,
        "description": "Get guardrails: recent failures, high-salience warnings, and skill pitfalls.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "skill": {"type": "string", "description": "Optional skill name to get specific guardrails for"},
            },
        },
    },
    "brain_skills": {
        "function": async_tool_brain_skills,
        "description": "Plan a task: recommend lightweight skill cards, guardrails, and execution strategy.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Task description to plan for"},
            },
            "required": ["task"],
        },
    },
    "skill_view": {
        "function": async_tool_skill_view,
        "description": "Progressively load one section of an installed skill, from a small card to a full procedure.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Installed skill name"},
                "section": {
                    "type": "string",
                    "enum": [
                        "card",
                        "summary",
                        "procedure",
                        "pitfalls",
                        "triggers",
                        "guardrails",
                        "graduated_steps",
                        "metadata",
                    ],
                    "default": "procedure",
                },
                "max_chars": {"type": "integer", "description": "Maximum text chars to return", "default": 12000},
            },
            "required": ["name"],
        },
    },
    "skill_asset": {
        "function": async_tool_skill_asset,
        "description": "Progressively load a specific versioned skill bundle asset by path.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Installed skill name"},
                "path": {"type": "string", "description": "Relative bundle asset path, for example examples/happy.md"},
                "max_chars": {"type": "integer", "description": "Maximum text chars to return", "default": 12000},
            },
            "required": ["name", "path"],
        },
    },
    "brain_encode": {
        "function": async_tool_brain_encode,
        "description": "Record a new memory (lesson, pattern, fact, or episode) into the brain.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Memory content (min 20 chars)"},
                "type": {"type": "string", "enum": ["lesson", "pattern", "fact", "episode"], "default": "episode"},
                "salience": {"type": "number", "description": "Importance 1-10 (default 5)", "default": 5.0},
            },
            "required": ["content"],
        },
    },
    "vault_inventory": {
        "function": tool_vault_inventory,
        "description": (
            "List metadata-only Vault secrets for agent reasoning. Returns key names, descriptions, "
            "categories, and agent access levels, never secret values. Use this before requesting a "
            "credential so the agent can choose an exact existing key or ask the user when ambiguous."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": [
                        "general",
                        "api",
                        "aws",
                        "auth",
                        "analytics",
                        "database",
                        "messaging",
                        "monitoring",
                        "payments",
                        "service",
                    ],
                    "description": "Optional Vault category filter.",
                },
                "access_level": {
                    "type": "string",
                    "enum": ["available", "ask", "manual"],
                    "description": "Optional agent access level filter.",
                },
            },
        },
    },
    "brain_vault": {
        "function": tool_brain_vault,
        "description": "Request task-scoped access to a secret from the encrypted vault.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Secret key name"},
                "reason": {"type": "string", "description": "Why this active task needs this exact secret"},
            },
            "required": ["key", "reason"],
        },
    },
    "vault_secret_prompt": {
        "function": tool_vault_secret_prompt,
        "description": (
            "Open a guided Vault form for the user to add a missing secret value. Call vault_inventory "
            "first; use this only when no suitable existing secret exists or the user explicitly asked "
            "to add a new key."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "key_name": {"type": "string", "description": "Secret key name to prefill"},
                "description": {"type": "string", "description": "Vault description to prefill"},
                "category": {
                    "type": "string",
                    "enum": [
                        "general",
                        "api",
                        "aws",
                        "auth",
                        "analytics",
                        "database",
                        "messaging",
                        "monitoring",
                        "payments",
                        "service",
                    ],
                    "default": "api",
                },
                "reason": {"type": "string", "description": "Why this active task needs the secret"},
            },
            "required": ["key_name"],
        },
    },
    "runtime_settings": {
        "function": tool_runtime_settings,
        "description": "Inspect the current runtime provider, auth status, and provider model mappings for the active user/workspace.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "provider": {
                    "type": "string",
                    "enum": ["anthropic", "openai"],
                    "description": "Optional provider to focus on; defaults to the effective provider.",
                },
            },
        },
    },
}


async def async_handle_request(request: dict) -> dict:
    """Handle a single MCP JSON-RPC request."""
    method = request.get("method", "")
    req_id = request.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "illo-brain", "version": "1.0.0"},
            },
        }

    if method == "notifications/initialized":
        return None  # notification, no response

    if method == "tools/list":
        tools_list = []
        for name, spec in TOOLS.items():
            tools_list.append({
                "name": name,
                "description": spec["description"],
                "inputSchema": spec["inputSchema"],
            })
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": tools_list},
        }

    if method == "tools/call":
        params = request.get("params", {})
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        if tool_name not in TOOLS:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": f"Unknown tool: {tool_name}"}],
                    "isError": True,
                },
            }

        try:
            # Map MCP argument names to function parameter names
            func = TOOLS[tool_name]["function"]
            arguments = dict(arguments)
            # Handle the 'type' → 'memory_type' rename for brain_encode
            if tool_name == "brain_encode" and "type" in arguments:
                arguments["memory_type"] = arguments.pop("type")
            result = await _maybe_await(func(**arguments))
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result, default=str)}],
                },
            }
        except Exception as e:
            logger.exception(f"Tool {tool_name} failed")
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": f"Error: {e}"}],
                    "isError": True,
                },
            }

    # Unknown method
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def handle_request(request: dict) -> dict:
    """Sync MCP protocol boundary for stdio/stdlib HTTP transports."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError("handle_request cannot run inside an active event loop; await async_handle_request")
    with asyncio.Runner() as runner:
        return runner.run(async_handle_request(request))


def run_stdio():
    """Run as MCP server over stdio (standard MCP transport)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        stream=sys.stderr,  # logs go to stderr, protocol goes to stdout
    )
    logger.info("MCP Brain Server starting (stdio transport)")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            response = handle_request(request)
            if response is not None:
                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON: {line[:100]}")
        except Exception as e:
            logger.exception(f"Request handling failed: {e}")


def run_http(port: int = 9877):
    """Run as HTTP server (for testing/debugging)."""
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode()
            try:
                request = json.loads(body)
                response = handle_request(request)
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                if response:
                    self.wfile.write(json.dumps(response).encode())
            except Exception as e:
                self.send_response(500)
                self.end_headers()
                self.wfile.write(str(e).encode())

        def log_message(self, format, *args):
            logger.info(format % args)

    logging.basicConfig(level=logging.INFO)
    server = HTTPServer(("127.0.0.1", port), Handler)
    logger.info(f"MCP Brain Server listening on http://127.0.0.1:{port}")
    server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MCP Brain Server")
    parser.add_argument("--http", action="store_true", help="Run as HTTP server instead of stdio")
    parser.add_argument("--port", type=int, default=9877, help="HTTP port (default 9877)")
    args = parser.parse_args()

    if args.http:
        run_http(args.port)
    else:
        run_stdio()
