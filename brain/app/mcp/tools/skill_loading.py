"""Progressive skill loading MCP tools."""
from __future__ import annotations

from typing import Any

from brain.app.mcp.tools.common import jsonish, truncate_text
from brain.app.mcp.tools.skill_common import (
    SKILL_VIEW_SECTIONS,
    skill_card,
    skill_digest_metadata,
    skill_summary,
    validate_skill_asset_path,
)
from brain.platform.providers.model_policy import DEFAULT_MODEL_TIER, normalize_model_tier


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
