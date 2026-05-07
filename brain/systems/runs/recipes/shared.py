"""Shared helpers for run recipes."""

from __future__ import annotations

from typing import Any


_WORKSPACE_ROOT_KEYS = ("resolved_workspace_root", "workspace_root", "worktree_path", "path", "local_path")


def _single_allowed_path(scope: dict[str, Any]) -> str | None:
    allowed_paths = scope.get("allowed_paths")
    if isinstance(allowed_paths, list) and len(allowed_paths) == 1:
        path = allowed_paths[0]
        if isinstance(path, str) and path.strip():
            return path.strip()
    return None


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
                path = resource.get("path")
                if isinstance(path, str) and path.strip():
                    return path.strip()
        scope = snapshot.get("permission_scope")
        if isinstance(scope, dict):
            path = _single_allowed_path(scope)
            if path:
                return path

    scope = workspace_ref.get("project_context_permission_scope")
    if isinstance(scope, dict):
        path = _single_allowed_path(scope)
        if path:
            return path

    return None
