"""Prompt-safe projections of run references.

Runtime references can contain complete Project snapshots, permission scopes,
and materialization metadata. Those payloads are useful for tools and audit
trails, but they must not be dumped into model instructions. This module keeps
the agent-visible prompt surface compact while preserving the important mount
and workspace facts.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
from typing import Any

_MAX_PROMPT_STRING_CHARS = 1_000
_MAX_PROMPT_DESCRIPTION_CHARS = 500
_MAX_PROMPT_ITEMS = 20
_PROJECT_RUNTIME_CONTEXT_KEY = "project_runtime_context"
_PROJECT_HEAVY_KEYS = {
    "allowed_paths",
    "denied_paths",
    "files",
    "folders",
    "forbidden_paths",
    "project_context_permission_scope",
    "project_context_snapshot",
    "project_runtime_context",
    "project_workspace_manifest",
    "resources",
    "uploaded_files",
}


def _clean_string(value: Any, *, limit: int = _MAX_PROMPT_STRING_CHARS) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) <= limit:
        return text
    return text[:limit] + f"...[truncated {len(text) - limit} chars]"


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _scalar_for_prompt(value: Any, *, limit: int = _MAX_PROMPT_STRING_CHARS) -> Any:
    if isinstance(value, str):
        return _clean_string(value, limit=limit)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return None


def _compact_sequence(
    value: Any,
    *,
    item_mapper,
    max_items: int = _MAX_PROMPT_ITEMS,
) -> dict[str, Any] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return None
    items = [item_mapper(item) for item in value[:max_items]]
    items = [item for item in items if item]
    return {
        "count": len(value),
        "items": items,
        **({"truncated": len(value) - max_items} if len(value) > max_items else {}),
    }


def _compact_mapping_scalars(value: Mapping[str, Any], keys: Sequence[str]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key in keys:
        scalar = _scalar_for_prompt(
            value.get(key),
            limit=_MAX_PROMPT_DESCRIPTION_CHARS if key == "description" else _MAX_PROMPT_STRING_CHARS,
        )
        if scalar not in (None, "", [], {}):
            compact[key] = scalar
    return compact


def _compact_workspace_entry(item: Any) -> dict[str, Any] | None:
    if isinstance(item, str):
        path = _clean_string(item)
        return {"path": path} if path else None
    if not isinstance(item, Mapping):
        return None
    return _compact_mapping_scalars(item, ("name", "mount_path", "label", "path"))


def _compact_resource(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, Mapping):
        return None
    return _compact_mapping_scalars(
        item,
        (
            "id",
            "kind",
            "type",
            "name",
            "label",
            "mount_path",
            "project_path",
            "repo",
            "branch",
            "path",
            "uri",
            "source",
            "description",
        ),
    )


def _compact_mount(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, Mapping):
        return None
    return _compact_mapping_scalars(
        item,
        (
            "id",
            "resource_id",
            "kind",
            "mount_path",
            "workspace_path",
            "resource_path",
            "source_path",
            "label",
            "repo",
        ),
    )


def _compact_permission_scope(scope: Any) -> dict[str, Any] | None:
    if not isinstance(scope, Mapping):
        return None
    compact = _compact_mapping_scalars(scope, ("mode", "permission_mode"))
    for key in ("resource_ids", "allowed_paths", "forbidden_paths", "denied_paths"):
        value = scope.get(key)
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
            continue
        compact[f"{key}_count"] = len(value)
        preview = [_clean_string(str(item)) for item in value[:5] if str(item or "").strip()]
        if preview:
            compact[f"{key}_preview"] = preview
    return compact or None


def _runtime_project_context(workspace_ref: Mapping[str, Any]) -> Mapping[str, Any]:
    return _as_mapping(workspace_ref.get(_PROJECT_RUNTIME_CONTEXT_KEY))


def _project_snapshot(workspace_ref: Mapping[str, Any]) -> Mapping[str, Any]:
    runtime = _runtime_project_context(workspace_ref)
    return _as_mapping(workspace_ref.get("project_context_snapshot")) or _as_mapping(
        runtime.get("project_context_snapshot")
    )


def _project_manifest(workspace_ref: Mapping[str, Any]) -> Mapping[str, Any]:
    runtime = _runtime_project_context(workspace_ref)
    return _as_mapping(workspace_ref.get("project_workspace_manifest")) or _as_mapping(
        runtime.get("project_workspace_manifest")
    )


def _project_materialization(workspace_ref: Mapping[str, Any]) -> Mapping[str, Any]:
    runtime = _runtime_project_context(workspace_ref)
    return _as_mapping(workspace_ref.get("project_context_materialization")) or _as_mapping(
        runtime.get("project_context_materialization")
    )


def _compact_project_materialization(materialization: Mapping[str, Any]) -> dict[str, Any]:
    compact = _compact_mapping_scalars(
        materialization,
        (
            "status",
            "empty_project",
            "seed_resource_count",
            "project_root_path_count",
            "project_root_file_count",
            "project_draft_path_count",
            "project_draft_file_count",
        ),
    )
    errors = materialization.get("errors")
    if isinstance(errors, Sequence) and not isinstance(errors, (str, bytes, bytearray)):
        compact["error_count"] = len(errors)
        preview = [_clean_string(str(error), limit=500) for error in errors[:3] if str(error or "").strip()]
        if preview:
            compact["errors"] = preview
    return compact


def compact_target_ref(target_ref: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a small prompt-safe target reference."""

    target = _as_mapping(target_ref)
    compact = _compact_mapping_scalars(
        target,
        (
            "kind",
            "idea_id",
            "thread_id",
            "event",
            "title",
            "status",
            "profile",
            "recipe",
            "source",
        ),
    )
    scope = _compact_permission_scope(target.get("project_context_permission_scope"))
    if scope:
        compact["project_context_permission_scope"] = scope
    snapshot = _as_mapping(target.get("project_context_snapshot"))
    if snapshot:
        compact["project"] = _compact_mapping_scalars(
            snapshot,
            ("project_id", "project_key", "name", "slug", "selected_profile_name", "source", "status"),
        )
        resources = snapshot.get("resources")
        if isinstance(resources, Sequence) and not isinstance(resources, (str, bytes, bytearray)):
            compact.setdefault("project", {})["resource_count"] = len(resources)
    return compact


def compact_workspace_ref(workspace_ref: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a prompt-safe workspace projection."""

    workspace = _as_mapping(workspace_ref)
    compact = _compact_mapping_scalars(
        workspace,
        (
            "workspace_root",
            "resolved_workspace_root",
            "name",
            "description",
            "project_id",
            "project_key",
            "slug",
            "selected_profile_name",
            "source",
            "validation_status",
            "status",
        ),
    )

    workspaces = _compact_sequence(workspace.get("workspaces"), item_mapper=_compact_workspace_entry)
    if workspaces:
        compact["workspaces"] = workspaces

    snapshot = _project_snapshot(workspace)
    if snapshot:
        compact["project"] = _compact_mapping_scalars(
            snapshot,
            (
                "project_id",
                "project_key",
                "name",
                "description",
                "slug",
                "selected_profile_name",
                "source",
                "status",
                "validation_status",
            ),
        )
        resources = _compact_sequence(snapshot.get("resources"), item_mapper=_compact_resource)
        if resources:
            compact["resources"] = resources
        scope = _compact_permission_scope(snapshot.get("permission_scope"))
        if scope:
            compact["permission_scope"] = scope

    materialization = _project_materialization(workspace)
    if materialization:
        compact["materialization"] = _compact_project_materialization(materialization)

    manifest = _project_manifest(workspace)
    if manifest:
        manifest_summary = _compact_mapping_scalars(
            manifest,
            ("project_key", "project_id", "workspace_root", "resolved_workspace_root"),
        )
        manifest_workspaces = _compact_sequence(manifest.get("workspaces"), item_mapper=_compact_workspace_entry)
        if manifest_workspaces:
            manifest_summary["workspaces"] = manifest_workspaces
        mounts = _compact_sequence(manifest.get("mounts"), item_mapper=_compact_mount)
        if mounts:
            manifest_summary["mounts"] = mounts
        if manifest_summary:
            compact["workspace_manifest"] = manifest_summary

    omitted = sorted(key for key in workspace.keys() if key in _PROJECT_HEAVY_KEYS)
    if omitted:
        compact["omitted_from_prompt"] = omitted
    return compact


def compact_prompt_value(title: str, value: Any) -> Any:
    lowered = title.strip().lower()
    if lowered == "workspace" and isinstance(value, Mapping):
        return compact_workspace_ref(value)
    if lowered == "target" and isinstance(value, Mapping):
        return compact_target_ref(value)
    return value


def prompt_json_block(title: str, value: Any) -> str:
    if not value:
        return ""
    compact = compact_prompt_value(title, value)
    if not compact:
        return ""
    return f"\n\n## {title}\n```json\n{json.dumps(compact, indent=2, default=str)}\n```"


__all__ = [
    "compact_prompt_value",
    "compact_target_ref",
    "compact_workspace_ref",
    "prompt_json_block",
]
