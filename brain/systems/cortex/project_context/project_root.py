"""Project-native root storage helpers.

A Project is always a folder-like root. Imported files, folders, and thread
attachments seed that root once; thread drafts then work against the root by
copy-on-write using the normal draft metadata.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
import hashlib
import json
import shutil

from brain.systems.cortex.project_context.drafts import PROJECT_HISTORY_DIR
from brain.systems.cortex.project_context.identity import PROJECT_IDENTITY_FIELDS
from brain.systems.cortex.project_context.workspace_manifest import (
    PROJECT_CONTEXT_DIR,
    PROJECT_CONTEXT_LOCAL_DIR,
)


PROJECT_ROOTS_DIR = "project-roots"
PROJECT_ROOT_RESOURCE_ID = "project-root"
PROJECT_ROOT_RESOURCE_KIND = "project_root"
PROJECT_ROOT_MOUNT_PATH = "/"
PROJECT_ROOT_IMPORTS_FILE = "imports.json"
PROJECT_KEY_FIELDS = PROJECT_IDENTITY_FIELDS


@dataclass(frozen=True)
class ProjectRootImportCandidate:
    source_path: Path
    relative_path: str
    resource_id: str | None = None

    @property
    def key(self) -> str:
        raw = f"{self.resource_id or ''}:{self.source_path}:{self.relative_path}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _clean_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _safe_segment(value: Any, *, fallback: str) -> str:
    text = _clean_text(value) or fallback
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in text.strip("/"))[:120]
    return safe or fallback


def _project_fingerprint(project_context: Mapping[str, Any], resources: Sequence[Mapping[str, Any]]) -> str:
    payload = {
        "name": project_context.get("name"),
        "description": project_context.get("description"),
        "resources": [
            {
                "id": resource.get("id"),
                "kind": resource.get("kind") or resource.get("type") or resource.get("resource_type"),
                "name": resource.get("name") or resource.get("label"),
                "path": resource.get("path") or resource.get("local_path") or resource.get("storage_path"),
                "uri": resource.get("uri") or resource.get("url") or resource.get("repo_url"),
                "repo": resource.get("repo"),
            }
            for resource in resources
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def project_key_from_context(
    project_context: Mapping[str, Any] | None,
    *,
    resources: Sequence[Mapping[str, Any]] | None = None,
    fallback: str | None = None,
) -> str:
    context = project_context if isinstance(project_context, Mapping) else {}
    resource_list = [resource for resource in (resources or []) if isinstance(resource, Mapping)]
    for key in PROJECT_KEY_FIELDS:
        value = _clean_text(context.get(key))
        if value:
            return _safe_segment(value, fallback="project")
    for resource in resource_list:
        value = _clean_text(resource.get("id"))
        if value:
            return _safe_segment(value, fallback="project")
    value = _clean_text(context.get("name") or context.get("title"))
    if value:
        return _safe_segment(value, fallback="project")
    if fallback:
        return _safe_segment(fallback, fallback="project")
    return f"project-{_project_fingerprint(context, resource_list)}"


def project_roots_parent(thread_workspace_root: Path) -> Path:
    root = Path(thread_workspace_root).expanduser()
    parent = root.parent
    if parent.name == "ideas":
        return parent.parent / PROJECT_ROOTS_DIR
    return parent / PROJECT_ROOTS_DIR


def project_root_path(thread_workspace_root: Path, project_key: str) -> Path:
    return project_roots_parent(thread_workspace_root) / _safe_segment(project_key, fallback="project")


def project_draft_root_path(thread_workspace_root: Path, project_key: str) -> Path:
    return (
        Path(thread_workspace_root).expanduser()
        / PROJECT_CONTEXT_DIR
        / PROJECT_CONTEXT_LOCAL_DIR
        / _safe_segment(project_key, fallback="project")
        / PROJECT_ROOT_RESOURCE_ID
    )


def safe_project_relative_path(value: Any, *, fallback: str) -> str | None:
    text = (_clean_text(value) or fallback).replace("\\", "/").strip("/")
    if not text:
        text = fallback
    path = PurePosixPath(text)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    safe_parts = [_safe_segment(part, fallback="item") for part in path.parts]
    return PurePosixPath(*safe_parts).as_posix()


def _imports_path(project_root: Path) -> Path:
    return Path(project_root) / PROJECT_HISTORY_DIR / PROJECT_ROOT_IMPORTS_FILE


def load_project_root_imports(project_root: Path) -> dict[str, Any]:
    path = _imports_path(project_root)
    if not path.exists():
        return {"schema_version": 1, "imports": {}}
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {"schema_version": 1, "imports": {}}
    if not isinstance(payload, dict):
        return {"schema_version": 1, "imports": {}}
    imports = payload.get("imports")
    if not isinstance(imports, dict):
        imports = {}
    return {"schema_version": 1, "imports": dict(imports)}


def save_project_root_imports(project_root: Path, metadata: Mapping[str, Any]) -> None:
    payload = {
        "schema_version": 1,
        "imports": dict(metadata.get("imports") if isinstance(metadata.get("imports"), Mapping) else {}),
    }
    path = _imports_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _unique_relative_path(project_root: Path, relative_path: str) -> str:
    candidate = PurePosixPath(relative_path)
    stem = candidate.stem
    suffix = candidate.suffix
    parent = candidate.parent
    index = 2
    while (project_root / candidate.as_posix()).exists():
        leaf = f"{stem}-{index}{suffix}" if stem else f"item-{index}{suffix}"
        candidate = parent / leaf if parent.as_posix() != "." else PurePosixPath(leaf)
        index += 1
    return candidate.as_posix()


def _copy_import_file(source_path: Path, target_path: Path) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_path, target_path)


def import_candidates_into_project_root(
    project_root: Path,
    candidates: Sequence[ProjectRootImportCandidate],
) -> dict[str, Any]:
    project_root = Path(project_root)
    project_root.mkdir(parents=True, exist_ok=True)
    metadata = load_project_root_imports(project_root)
    imports = dict(metadata.get("imports") or {})
    imported: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []

    for candidate in candidates:
        source_path = Path(candidate.source_path).expanduser()
        if not source_path.exists() or not source_path.is_file() or source_path.is_symlink():
            continue
        existing = imports.get(candidate.key)
        if isinstance(existing, Mapping):
            skipped.append({
                "source_path": str(source_path),
                "relative_path": str(existing.get("relative_path") or candidate.relative_path),
            })
            continue

        relative_path = candidate.relative_path
        target_path = project_root / relative_path
        if target_path.exists() or target_path.is_symlink():
            relative_path = _unique_relative_path(project_root, relative_path)
            target_path = project_root / relative_path
        _copy_import_file(source_path, target_path)
        imports[candidate.key] = {
            "source_path": str(source_path),
            "relative_path": relative_path,
            "resource_id": candidate.resource_id,
        }
        imported.append({"source_path": str(source_path), "relative_path": relative_path})

    metadata["imports"] = imports
    save_project_root_imports(project_root, metadata)
    return {"imported": imported, "skipped": skipped}


def directory_import_candidates(
    source_root: Path,
    *,
    resource_id: str | None = None,
    prefix: str | None = None,
) -> list[ProjectRootImportCandidate]:
    source_root = Path(source_root).expanduser()
    if not source_root.exists() or not source_root.is_dir():
        return []
    candidates: list[ProjectRootImportCandidate] = []
    for path in sorted(source_root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(source_root).as_posix()
        if any(part in {PROJECT_HISTORY_DIR, PROJECT_CONTEXT_DIR} for part in Path(relative).parts):
            continue
        if prefix:
            relative = f"{prefix.strip('/')}/{relative}"
        safe_relative = safe_project_relative_path(relative, fallback=path.name)
        if safe_relative:
            candidates.append(ProjectRootImportCandidate(path, safe_relative, resource_id=resource_id))
    return candidates
