"""Skill planning and progressive-loading MCP tool implementations."""
from __future__ import annotations

import json
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from sqlalchemy import text

from brain.app.mcp.tools.common import jsonish, truncate_text
from brain.platform.providers.model_policy import DEFAULT_MODEL_TIER, normalize_model_tier


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


def skill_digest_metadata(skill: Any) -> dict[str, Any]:
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


def skill_card(skill: Any) -> dict[str, str]:
    return {
        "name": getattr(skill, "name", "") or "",
        "description": getattr(skill, "description", "") or "",
    }


def compact_skill_item(item: Any) -> str:
    if isinstance(item, dict):
        for key in ("text", "pattern", "action", "condition", "summary", "description"):
            value = item.get(key)
            if value:
                return str(value)
        return json.dumps(item, ensure_ascii=False, default=str)
    return str(item)


def compact_skill_items(value: Any, *, limit: int = 3, max_chars: int = 180) -> list[str]:
    items = jsonish(value, [])
    if not isinstance(items, list):
        return []
    compact: list[str] = []
    for item in items[:limit]:
        text_value, _ = truncate_text(compact_skill_item(item).strip(), max_chars)
        if text_value:
            compact.append(text_value)
    return compact


def skill_summary(skill: Any, max_chars: int) -> tuple[str, bool]:
    parts: list[str] = []
    description = str(getattr(skill, "description", "") or "").strip()
    if description:
        parts.append(f"Description: {description}")

    procedure = str(getattr(skill, "procedure", "") or "").strip()
    if procedure:
        preview, preview_truncated = truncate_text(procedure, 900)
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
        items = compact_skill_items(getattr(skill, attr, None))
        if items:
            parts.append(label + ":\n" + "\n".join(f"- {item}" for item in items))

    if not parts:
        parts.append(f"{getattr(skill, 'name', '') or 'Skill'}: no summary content is available.")

    return truncate_text("\n\n".join(parts), max_chars)


def validate_skill_asset_path(path: str) -> str:
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


async def _fetch_skill_card(uow: Any, name: str, *, session_execute: Any) -> Any:
    result = await session_execute(uow.session, text("""
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

        count_result = await session_execute(uow.session, text(
            "SELECT COUNT(*) as cnt FROM skills WHERE NOT archived"
        ))
        count_row = count_result.mappings().one()
        skill_count = count_row["cnt"]

        if emb_str is None:
            limit_clause = "" if skill_count <= small_skillset_threshold else "LIMIT 15"
            matching_result = await session_execute(uow.session, text(f"""
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
        elif skill_count <= small_skillset_threshold:
            matching_result = await session_execute(uow.session, text("""
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
            matching_result = await session_execute(uow.session, text("""
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

        if task_emb is None:
            guardrail_memories = []
        else:
            guardrail_memories = await maybe_await(uow.memories.guardrail_memories_for_task(
                task_embedding=task_emb,
                limit=5,
            ))

        if _looks_like_workspace_app_task(task):
            workspace_app_skill = await _fetch_skill_card(
                uow,
                "build-workspace-app",
                session_execute=session_execute,
            )
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
            domain_skill = await _fetch_skill_card(
                uow,
                "manage-domains",
                session_execute=session_execute,
            )
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
                assets = await maybe_await(uow.skill_bundles.list_assets(version_id))
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

    ranked_recommendations: list[dict[str, Any]] = []
    for skill in matching_skills:
        sim = float(skill["skill_match"]) if skill["skill_match"] else 0
        if task_emb is not None and skill_count > small_skillset_threshold and sim <= 0.3:
            continue

        success_rate = float(skill["success_rate"]) if skill["success_rate"] else 0
        use_count = int(skill["use_count"] or 0)
        maturity = skill["maturity"] or "emerging"
        centroid_match = skill.get("centroid_match")
        centroid_count = int(skill.get("centroid_count") or 0)
        if centroid_match is not None and centroid_count >= 3:
            semantic_score = float(centroid_match) * 0.7 + sim * 0.3
        else:
            semantic_score = sim

        if task_emb is None and sim >= 1.0:
            composite = 1.1
        elif task_emb is None:
            composite = (
                float(skill["confidence"] or 0) * 0.6
                + success_rate * 0.3
                + min(use_count, 20) / 100
            )
        else:
            composite = semantic_score

        card = {
            "name": skill["name"],
            "description": skill["description"] or "",
            "loaded_sections": ["catalog"],
            "available_sections": list(SKILL_VIEW_SECTIONS),
            "load_tools": {
                "card": {
                    "tool": "skill_view",
                    "arguments": {"name": skill["name"], "section": "card"},
                },
                "summary": {
                    "tool": "skill_view",
                    "arguments": {"name": skill["name"], "section": "summary"},
                },
                "procedure": {
                    "tool": "skill_view",
                    "arguments": {"name": skill["name"], "section": "procedure"},
                },
                "assets": {
                    "tool": "skill_asset",
                    "arguments": {"name": skill["name"], "path": "examples/..."},
                },
            },
        }
        triggers = _coerce_triggers(skill.get("triggers"))
        trigger_score, matched_trigger = _trigger_match(task, triggers)
        if trigger_score:
            composite = max(composite, trigger_score)
        bundle_version_id = skill.get("bundle_version_id")
        if bundle_version_id:
            available_assets = asset_paths_by_version.get(int(bundle_version_id), [])
            if available_assets:
                card["assets"] = available_assets[:20]
                card["load_tools"]["assets"]["available_paths"] = [
                    asset["path"] for asset in available_assets[:20]
                ]
        ranked_recommendations.append({
            "card": card,
            "sort_score": composite,
            "maturity": maturity,
            "success_rate": success_rate,
            "matched_trigger": matched_trigger,
        })

    ranked_recommendations.sort(key=lambda item: item["sort_score"], reverse=True)
    recommended = [item["card"] for item in ranked_recommendations]
    guardrails = [memory["content"][:200] for memory in guardrail_memories]

    strategy = "full_pipeline"
    if ranked_recommendations:
        top = ranked_recommendations[0]
        if top["maturity"] == "expert" and top["success_rate"] > 0.85:
            strategy = "direct"
        elif top["maturity"] in ("emerging", "developing"):
            strategy = "investigate_first"

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


async def skill_view_tool(
    name: str,
    section: str = "procedure",
    max_chars: int = 12000,
    *,
    unit_of_work_cls: Any,
    maybe_await: Any,
) -> dict:
    """Load a specific skill section on demand."""
    section = (section or "procedure").strip().lower()
    allowed = set(SKILL_VIEW_SECTIONS)
    if section not in allowed:
        return {
            "error": f"Unknown skill section: {section}",
            "allowed_sections": sorted(allowed),
        }

    async with unit_of_work_cls() as uow:
        skill = await maybe_await(uow.skills.get_by_name(name))
        if skill is None:
            return {"error": f"Skill '{name}' not found"}

        if section == "card":
            return {
                **skill_card(skill),
                "section": section,
                "loaded_sections": [section],
            }

        metadata = skill_digest_metadata(skill)
        payload: dict[str, Any] = {
            "name": skill.name,
            "section": section,
            "loaded_sections": [section],
            **metadata,
        }

        if section == "summary":
            text_value, truncated = skill_summary(skill, max_chars)
            payload.update({
                "description": skill.description or "",
                "content_type": "text/markdown",
                "content": text_value,
                "truncated": truncated,
            })
        elif section == "procedure":
            text_value, truncated = truncate_text(skill.procedure or "", max_chars)
            payload.update({
                "content_type": "text/markdown",
                "content": text_value,
                "truncated": truncated,
            })
        elif section == "pitfalls":
            payload["items"] = jsonish(skill.pitfalls, [])
        elif section == "triggers":
            payload["items"] = jsonish(skill.triggers, [])
        elif section == "guardrails":
            payload["items"] = jsonish(skill.guardrails, [])
        elif section == "graduated_steps":
            payload["items"] = jsonish(skill.graduated_steps, [])
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


async def skill_asset_tool(
    name: str,
    path: str,
    max_chars: int = 12000,
    *,
    unit_of_work_cls: Any,
    maybe_await: Any,
) -> dict:
    """Load a specific asset from the installed skill bundle version."""
    try:
        safe_path = validate_skill_asset_path(path)
    except ValueError as exc:
        return {"error": str(exc)}

    async with unit_of_work_cls() as uow:
        skill = await maybe_await(uow.skills.get_by_name(name))
        if skill is None:
            return {"error": f"Skill '{name}' not found"}
        if not skill.bundle_version_id:
            return {
                "error": f"Skill '{name}' is not backed by a bundle version",
                "name": skill.name,
                **skill_digest_metadata(skill),
            }

        assets = await maybe_await(uow.skill_bundles.list_assets(skill.bundle_version_id))
        asset = next((item for item in assets if item.path == safe_path), None)
        if asset is None:
            return {
                "error": f"Skill asset not found: {safe_path}",
                "name": skill.name,
                "path": safe_path,
                "available_assets": [item.path for item in assets[:50]],
                **skill_digest_metadata(skill),
            }

        content = asset.content_text
        truncated = False
        if content is not None:
            content, truncated = truncate_text(content, max_chars)

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
            **skill_digest_metadata(skill),
        }
