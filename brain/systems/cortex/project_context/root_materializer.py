"""Project-native root draft materialization helpers."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
import os
import shutil

from brain.systems.cortex.project_context.drafts import build_file_manifest, load_draft_metadata, sync_draft_from_root
from brain.systems.cortex.project_context.project_root import (
    PROJECT_ROOT_MOUNT_PATH,
    PROJECT_ROOT_RESOURCE_ID,
    PROJECT_ROOT_RESOURCE_KIND,
    ProjectRootImportCandidate,
    directory_import_candidates,
    project_draft_root_path,
    project_key_from_context,
    project_root_path,
    safe_project_relative_path,
)
from brain.systems.cortex.project_context.root_imports import import_candidates_into_project_root_versioned


ProjectSeedPredicate = Callable[[dict[str, Any]], bool]
WorkspaceEntryFactory = Callable[[dict[str, Any], Path | str], dict[str, str]]


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


def should_use_existing_resource_path(existing_path: str | None, workspace_root: Path) -> Path | None:
    if not existing_path:
        return None
    path = Path(existing_path).expanduser()
    if not path.exists():
        return None
    if path_is_relative_to(path, workspace_root):
        return path
    if is_managed_project_context_path(path):
        return None
    return path


def runtime_workspace_path(path: Path) -> Path:
    return path if path.is_dir() else path.parent


def _project_root_alias_paths(
    project_context: Mapping[str, Any],
    resources: Sequence[Mapping[str, Any]],
    *,
    workspace_root: Path,
    canonical_key: str,
) -> list[Path]:
    canonical_root = project_root_path(workspace_root, canonical_key)
    values: list[Any] = [
        project_context.get("slug"),
        project_context.get("name"),
        project_context.get("title"),
        project_context.get("project_key"),
    ]
    for resource in resources:
        if not isinstance(resource, Mapping):
            continue
        values.extend([resource.get("project_key"), resource.get("slug")])

    aliases: list[Path] = []
    for value in values:
        text = clean_text(value)
        if not text or text == canonical_key:
            continue
        alias = project_root_path(workspace_root, text)
        if alias != canonical_root and alias not in aliases:
            aliases.append(alias)
    return aliases


def _adopt_existing_project_root_alias(source_root: Path, alias_roots: Sequence[Path]) -> str | None:
    if build_file_manifest(source_root):
        return None
    for alias_root in alias_roots:
        if not alias_root.exists() or not alias_root.is_dir() or alias_root == source_root:
            continue
        if not build_file_manifest(alias_root):
            continue
        source_root.mkdir(parents=True, exist_ok=True)
        for item in alias_root.iterdir():
            target = source_root / item.name
            if target.exists():
                continue
            if item.is_dir() and not item.is_symlink():
                shutil.copytree(item, target, symlinks=False)
            elif item.is_file() and not item.is_symlink():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, target)
        return str(alias_root)
    return None


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
        existing_path = should_use_existing_resource_path(value, workspace_root)
        if existing_path:
            return existing_path, True

    scoped_paths: list[Path] = []
    path_texts = [*_path_texts_from_uploaded_files(resource), *_path_texts_from_resource_scope(resource)]
    for value in path_texts:
        existing_path = should_use_existing_resource_path(value, workspace_root)
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


def materialize_project_native_root(
    project_context: dict[str, Any],
    resources: Sequence[dict[str, Any]],
    *,
    workspace_root: Path,
    run_id: int | None,
    actor_id: str | None = None,
    org_id: str | None = None,
    is_project_native_seed: ProjectSeedPredicate,
    workspace_entry_from_resource: WorkspaceEntryFactory,
) -> tuple[dict[str, Any], dict[str, str]]:
    project_key = project_key_from_context(
        project_context,
        resources=resources,
        fallback=f"run-{run_id}" if run_id else None,
    )
    source_root = project_root_path(workspace_root, project_key)
    source_root.mkdir(parents=True, exist_ok=True)
    adopted_from_root = _adopt_existing_project_root_alias(
        source_root,
        _project_root_alias_paths(
            project_context,
            resources,
            workspace_root=workspace_root,
            canonical_key=project_key,
        ),
    )

    import_candidates: list[ProjectRootImportCandidate] = []
    for resource in resources:
        import_candidates.extend(
            project_native_import_candidates(
                resource,
                workspace_root=workspace_root,
                is_project_native_seed=is_project_native_seed,
            )
        )
    import_summary = import_candidates_into_project_root_versioned(
        source_root,
        import_candidates,
        run_id=run_id,
        actor_id=actor_id,
        org_id=org_id,
    )

    draft_root = project_draft_root_path(workspace_root, project_key)
    had_base_manifest = bool(load_draft_metadata(draft_root).get("base_manifest"))
    sync_result = sync_draft_from_root(source_root, draft_root)
    root_manifest = build_file_manifest(source_root)
    draft_manifest = build_file_manifest(draft_root)
    root_file_count = sum(1 for entry in root_manifest.values() if entry.get("kind") == "file")
    draft_file_count = sum(1 for entry in draft_manifest.values() if entry.get("kind") == "file")
    draft_status = {
        "updated_from_root": sync_result.copied,
        "removed_from_root": sync_result.removed,
        "conflicts": sync_result.conflicts,
        "out_of_date": sync_result.out_of_date,
    } if had_base_manifest else {}

    materialization: dict[str, Any] = {
        "status": "ready",
        "provider": "project_native",
        "kind": PROJECT_ROOT_RESOURCE_KIND,
        "path": str(draft_root),
        "workspace_path": str(draft_root),
        "source_path": str(source_root),
        "draft": True,
        "project_key": project_key,
        "root_empty": not root_manifest,
        "root_path_count": len(root_manifest),
        "root_file_count": root_file_count,
        "draft_path_count": len(draft_manifest),
        "draft_file_count": draft_file_count,
    }
    if any(import_summary.values()):
        materialization["imports"] = import_summary
    if adopted_from_root:
        materialization["adopted_from_root"] = adopted_from_root
    if draft_status and any(draft_status.values()):
        materialization["draft_status"] = draft_status

    resource = {
        "id": PROJECT_ROOT_RESOURCE_ID,
        "kind": PROJECT_ROOT_RESOURCE_KIND,
        "name": "Project root",
        "label": "Project root",
        "mount_path": PROJECT_ROOT_MOUNT_PATH,
        "path": str(draft_root),
        "workspace_path": str(draft_root),
        "source_path": str(source_root),
        "materialization": materialization,
    }
    return resource, workspace_entry_from_resource(resource, draft_root)
