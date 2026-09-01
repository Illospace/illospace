"""Resource-to-Project-root import planning."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from enum import Enum
from pathlib import Path
from typing import Any
import os

from brain.systems.cortex.project_context.project_root import (
    ProjectRootImportCandidate,
    directory_import_candidates,
    safe_project_relative_path,
)


ProjectSeedPredicate = Callable[[dict[str, Any]], bool]


class ResourcePathAccess(str, Enum):
    READ_ONLY = "read_only"
    WRITE = "write"


def clean_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.expanduser().resolve().relative_to(parent.expanduser().resolve())
        return True
    except Exception:
        return False


def is_managed_project_context_path(path: Path) -> bool:
    return ".illo-project-context" in path.expanduser().parts


def should_use_existing_resource_path(
    existing_path: str | None,
    workspace_root: Path,
    *,
    access: ResourcePathAccess,
) -> Path | None:
    """Return an existing path without granting cross-workspace write access."""
    if not existing_path:
        return None
    path = Path(existing_path).expanduser()
    if not path.exists():
        return None
    if path_is_relative_to(path, workspace_root):
        return path
    if is_managed_project_context_path(path) and access is not ResourcePathAccess.READ_ONLY:
        return None
    return path


def runtime_workspace_path(path: Path) -> Path:
    return path if path.is_dir() else path.parent


def _path_texts_from_uploaded_files(resource: Mapping[str, Any]) -> list[str]:
    paths: list[str] = []
    uploaded_files = resource.get("uploaded_files")
    if not isinstance(uploaded_files, list):
        return paths
    for item in uploaded_files:
        if not isinstance(item, Mapping):
            continue
        value = clean_text(item.get("storage_path") or item.get("path") or item.get("local_path"))
        if value:
            paths.append(value)
    return paths


def _path_texts_from_resource_scope(resource: Mapping[str, Any]) -> list[str]:
    paths: list[str] = []
    for key in ("allowed_paths", "files", "folders"):
        values = resource.get(key)
        if not isinstance(values, list):
            continue
        for item in values:
            value = clean_text(item)
            if value:
                paths.append(value)
    return paths


def resource_id(resource: Mapping[str, Any]) -> str | None:
    return clean_text(resource.get("id") or resource.get("name") or resource.get("label"))


def _common_runtime_workspace_path(paths: list[Path]) -> Path | None:
    if not paths:
        return None
    workspace_paths = [runtime_workspace_path(path).resolve() for path in paths]
    try:
        common = Path(os.path.commonpath([str(path) for path in workspace_paths]))
    except ValueError:
        return None
    return common if common.exists() else None


def backend_readable_resource_path(
    resource: Mapping[str, Any],
    *,
    workspace_root: Path,
) -> tuple[Path | None, bool]:
    for key in ("path", "local_path", "storage_path"):
        value = clean_text(resource.get(key))
        existing_path = should_use_existing_resource_path(
            value,
            workspace_root,
            access=ResourcePathAccess.READ_ONLY,
        )
        if existing_path:
            return existing_path, True

    scoped_paths: list[Path] = []
    path_texts = [*_path_texts_from_uploaded_files(resource), *_path_texts_from_resource_scope(resource)]
    for value in path_texts:
        existing_path = should_use_existing_resource_path(
            value,
            workspace_root,
            access=ResourcePathAccess.READ_ONLY,
        )
        if existing_path:
            scoped_paths.append(existing_path)

    common_path = _common_runtime_workspace_path(scoped_paths)
    if common_path:
        return common_path, True
    return None, bool(path_texts)


def _uploaded_file_import_candidates(
    resource: Mapping[str, Any],
    *,
    workspace_root: Path,
) -> list[ProjectRootImportCandidate]:
    uploaded_files = resource.get("uploaded_files")
    if not isinstance(uploaded_files, list):
        return []

    candidates: list[ProjectRootImportCandidate] = []
    resource_key = resource_id(resource)
    for item in uploaded_files:
        if not isinstance(item, Mapping):
            continue
        source = should_use_existing_resource_path(
            clean_text(item.get("storage_path") or item.get("path") or item.get("local_path")),
            workspace_root,
            access=ResourcePathAccess.READ_ONLY,
        )
        if not source or not source.is_file():
            continue
        relative_path = safe_project_relative_path(
            item.get("relative_path") or item.get("filename") or item.get("name") or source.name,
            fallback=source.name,
        )
        if relative_path:
            candidates.append(ProjectRootImportCandidate(source, relative_path, resource_id=resource_key))
    return candidates


def _direct_path_import_candidates(
    resource: Mapping[str, Any],
    *,
    workspace_root: Path,
) -> list[ProjectRootImportCandidate]:
    source, checked = backend_readable_resource_path(resource, workspace_root=workspace_root)
    if not source or not checked:
        return []
    resource_key = resource_id(resource)
    if source.is_file():
        relative_path = safe_project_relative_path(
            resource.get("relative_path") or resource.get("name") or resource.get("label") or source.name,
            fallback=source.name,
        )
        return [ProjectRootImportCandidate(source, relative_path, resource_id=resource_key)] if relative_path else []
    return directory_import_candidates(source, resource_id=resource_key)


def project_native_import_candidates(
    resource: dict[str, Any],
    *,
    workspace_root: Path,
    is_project_native_seed: ProjectSeedPredicate,
) -> list[ProjectRootImportCandidate]:
    if not is_project_native_seed(resource):
        return []
    uploaded = _uploaded_file_import_candidates(resource, workspace_root=workspace_root)
    if uploaded:
        return uploaded
    return _direct_path_import_candidates(resource, workspace_root=workspace_root)


__all__ = [
    "ProjectSeedPredicate",
    "ResourcePathAccess",
    "backend_readable_resource_path",
    "clean_text",
    "path_is_relative_to",
    "project_native_import_candidates",
    "resource_id",
    "runtime_workspace_path",
    "should_use_existing_resource_path",
]
