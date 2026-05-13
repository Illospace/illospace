"""Shared helpers for run recipes."""

from __future__ import annotations

from pathlib import PurePath
from typing import Any


_WORKSPACE_ROOT_KEYS = ("resolved_workspace_root", "workspace_root", "worktree_path", "path", "local_path")


def _single_allowed_path(scope: dict[str, Any]) -> str | None:
    allowed_paths = scope.get("allowed_paths")
    if isinstance(allowed_paths, list) and len(allowed_paths) == 1:
        path = allowed_paths[0]
        if isinstance(path, str) and path.strip():
            return path.strip()
    return None


def _looks_like_file_path(path: str) -> bool:
    return bool(PurePath(path).suffix)


def _resource_workspace_path(resource: dict[str, Any]) -> str | None:
    path = resource.get("path")
    if not isinstance(path, str) or not path.strip():
        return None
    resource_kind = str(resource.get("kind") or resource.get("type") or "").strip().lower()
    if resource_kind in {"file", "attachment"}:
        return None
    if resource_kind in {"repo", "repository", "directory", "folder", "workspace"}:
        return path.strip()
    return None if _looks_like_file_path(path) else path.strip()


def workspace_root_from_ref(workspace_ref: dict[str, Any]) -> str | None:
    """Return the concrete workspace root represented by a runtime workspace reference.

    Thread project context can arrive as a durable project-context snapshot without a
    top-level workspace_root field. When it represents a single materialized resource,
    use that resource path so tools operate in the thread's shared project workspace.
    """
    for key in _WORKSPACE_ROOT_KEYS:
        value = workspace_ref.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    snapshot = workspace_ref.get("project_context_snapshot")
    if isinstance(snapshot, dict):
        resources = snapshot.get("resources")
        if isinstance(resources, list) and len(resources) == 1:
            resource = resources[0]
            if isinstance(resource, dict):
                path = _resource_workspace_path(resource)
                if path:
                    return path
        scope = snapshot.get("permission_scope")
        if isinstance(scope, dict):
            path = _single_allowed_path(scope)
            if path and not _looks_like_file_path(path):
                return path

    scope = workspace_ref.get("project_context_permission_scope")
    if isinstance(scope, dict):
        path = _single_allowed_path(scope)
        if path and not _looks_like_file_path(path):
            return path

    return None
