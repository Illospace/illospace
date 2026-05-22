"""Shared helpers for MCP skill tools."""
from __future__ import annotations

import json
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from brain.app.mcp.tools.common import jsonish, truncate_text


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
