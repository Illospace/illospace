"""Shared helpers for run recipes."""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path, PurePath
from typing import Any


_WORKSPACE_ROOT_KEYS = ("resolved_workspace_root", "workspace_root", "worktree_path", "path", "local_path")


@dataclass(frozen=True)
class ProjectRuntimeWorkspace:
    """Agent-facing workspace projection for a Project-backed run."""

    workspace_root: str | None = None
    allowed_workspaces: list[dict[str, str]] = field(default_factory=list)


def _workspace_entry_name(path: str, *candidates: Any) -> str:
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return os.path.basename(path.rstrip(os.sep)) or path


def _add_workspace_entry(
    registry: list[dict[str, str]],
    seen_paths: set[str],
    path: Any,
    *name_candidates: Any,
) -> None:
    if not isinstance(path, str) or not path.strip():
        return
    clean_path = path.strip()
    if clean_path in seen_paths:
        return
    seen_paths.add(clean_path)
    registry.append({
        "name": _workspace_entry_name(clean_path, *name_candidates),
        "path": clean_path,
    })


def _looks_like_file_path(path: str) -> bool:
    return bool(PurePath(path).suffix)


def _workspace_directory_path(path: str | None) -> str | None:
    if not isinstance(path, str) or not path.strip():
        return None
    candidate = path.strip()
    local_path = Path(candidate).expanduser()
    try:
        if local_path.exists():
            return candidate if local_path.is_dir() else None
    except OSError:
        return None
    if _looks_like_file_path(candidate):
        return None
    return candidate


def _resource_workspace_path(resource: dict[str, Any]) -> str | None:
    materialization = resource.get("materialization")
    if isinstance(materialization, dict):
        workspace_path = _workspace_directory_path(materialization.get("workspace_path"))
        if workspace_path:
            return workspace_path

    path = resource.get("path")
    if not isinstance(path, str) or not path.strip():
        return None
    resource_kind = str(resource.get("kind") or resource.get("type") or "").strip().lower()
    if resource_kind in {"file", "attachment"}:
        return None
    if resource_kind in {"repo", "repository", "directory", "folder", "workspace"}:
        return _workspace_directory_path(path)
    return _workspace_directory_path(path)


def _single_allowed_path(scope: dict[str, Any]) -> str | None:
    allowed_paths = scope.get("allowed_paths")
    if isinstance(allowed_paths, list) and len(allowed_paths) == 1:
        path = allowed_paths[0]
        if isinstance(path, str) and path.strip():
            return path.strip()
    return None


def project_runtime_workspace_from_ref(workspace_ref: dict[str, Any]) -> ProjectRuntimeWorkspace:
    """Return the agent-facing workspace set represented by a runtime workspace reference."""

    workspace_ref = workspace_ref if isinstance(workspace_ref, dict) else {}
    allowed_workspaces: list[dict[str, str]] = []
    seen_paths: set[str] = set()

    workspace_manifest = workspace_ref.get("project_workspace_manifest")
    if isinstance(workspace_manifest, dict):
        for item in workspace_manifest.get("workspaces") or []:
            if isinstance(item, dict):
                _add_workspace_entry(
                    allowed_workspaces,
                    seen_paths,
                    item.get("path"),
                    item.get("mount_path"),
                    item.get("name"),
                    item.get("label"),
                )
        workspace_root = None
        for key in _WORKSPACE_ROOT_KEYS:
            path = _workspace_directory_path(workspace_ref.get(key))
            if path:
                workspace_root = path
                break
        if workspace_root is None:
            workspace_root = _workspace_directory_path(workspace_manifest.get("workspace_root"))
        if workspace_root is None and allowed_workspaces:
            workspace_root = allowed_workspaces[0]["path"]
        if workspace_root is not None:
            _add_workspace_entry(allowed_workspaces, seen_paths, workspace_root)
        if allowed_workspaces:
            return ProjectRuntimeWorkspace(
                workspace_root=workspace_root,
                allowed_workspaces=allowed_workspaces,
            )

    for item in workspace_ref.get("workspaces") or []:
        if isinstance(item, str):
            _add_workspace_entry(allowed_workspaces, seen_paths, _workspace_directory_path(item))
        elif isinstance(item, dict):
            _add_workspace_entry(
                allowed_workspaces,
                seen_paths,
                _workspace_directory_path(item.get("path")),
                item.get("mount_path"),
                item.get("name"),
                item.get("label"),
            )

    snapshot = workspace_ref.get("project_context_snapshot")
    if isinstance(snapshot, dict):
        for resource in snapshot.get("resources") or []:
            if not isinstance(resource, dict):
                continue
            _add_workspace_entry(
                allowed_workspaces,
                seen_paths,
                _resource_workspace_path(resource),
                resource.get("mount_path"),
                resource.get("project_path"),
                resource.get("repo"),
                resource.get("name"),
                resource.get("label"),
            )

    if not allowed_workspaces:
        scope_candidates = []
        if isinstance(snapshot, dict) and isinstance(snapshot.get("permission_scope"), dict):
            scope_candidates.append(snapshot["permission_scope"])
        scope = workspace_ref.get("project_context_permission_scope")
        if isinstance(scope, dict):
            scope_candidates.append(scope)
        for candidate_scope in scope_candidates:
            allowed_paths = candidate_scope.get("allowed_paths")
            if not isinstance(allowed_paths, list):
                continue
            for path in allowed_paths:
                _add_workspace_entry(allowed_workspaces, seen_paths, _workspace_directory_path(path))

    workspace_root = None
    for key in _WORKSPACE_ROOT_KEYS:
        path = _workspace_directory_path(workspace_ref.get(key))
        if path:
            workspace_root = path
            break
    if workspace_root is None and allowed_workspaces:
        workspace_root = allowed_workspaces[0]["path"]

    if workspace_root is not None:
        _add_workspace_entry(allowed_workspaces, seen_paths, workspace_root)

    return ProjectRuntimeWorkspace(
        workspace_root=workspace_root,
        allowed_workspaces=allowed_workspaces,
    )


def workspace_root_from_ref(workspace_ref: dict[str, Any]) -> str | None:
    """Return the concrete workspace root represented by a runtime workspace reference."""

    projected = project_runtime_workspace_from_ref(workspace_ref)
    if projected.workspace_root:
        return projected.workspace_root

    for key in _WORKSPACE_ROOT_KEYS:
        path = _workspace_directory_path(workspace_ref.get(key))
        if path:
            return path

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
            path = _workspace_directory_path(_single_allowed_path(scope))
            if path:
                return path

    scope = workspace_ref.get("project_context_permission_scope")
    if isinstance(scope, dict):
        path = _workspace_directory_path(_single_allowed_path(scope))
        if path:
            return path

    return None


__all__ = [
    "ProjectRuntimeWorkspace",
    "project_runtime_workspace_from_ref",
    "workspace_root_from_ref",
]
