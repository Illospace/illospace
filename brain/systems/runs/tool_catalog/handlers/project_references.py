"""Cross-Project search and read-only reference mount helpers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath
from typing import Any
import json
import re

from brain.systems.cortex.project_context.identity import stamped_project_context
from brain.systems.cortex.project_context.project_root import project_key_from_context, project_root_path
from brain.systems.cortex.project_context.profiles import profile_to_read
from brain.systems.runs.execution_context import _agent_context
from brain.systems.runs.project_execution_env import _current_workspace_root_hint
from brain.systems.runs.tool_catalog.handlers.common import _patched_workspace_root


PROJECT_FILE_SEARCH_DEFAULT_LIMIT = 20
PROJECT_FILE_SEARCH_MAX_LIMIT = 100

_PROJECT_REFERENCE_MOUNT_ROOT = "/references"
_PROJECT_FILE_SEARCH_MAX_BYTES = 512 * 1024
_TEXT_FILE_EXTENSIONS = {
    ".csv",
    ".css",
    ".html",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".mdx",
    ".py",
    ".rb",
    ".rst",
    ".sql",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
_PROJECT_INTERNAL_DIRS = {".illo-project-draft", ".illo-project-history"}


def _clean_query(value: Any) -> str:
    return str(value or "").strip().lower()


def _query_terms(value: Any) -> list[str]:
    return [term for term in re.split(r"\s+", _clean_query(value)) if term]


def profile_matches_query(profile, query: str | None) -> bool:
    terms = _query_terms(query)
    if not terms:
        return True
    blob = _profile_search_blob(profile)
    return all(term in blob for term in terms)


def limit_value(value: Any, *, default: int, maximum: int) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, min(limit, maximum))


def search_project_root_files(
    profile,
    *,
    query: str | None,
    path: str | None,
    paths: list[str] | None,
    glob: str | None,
    limit: int,
) -> dict[str, Any]:
    root_path = _profile_root_path(profile)
    if root_path is None:
        return {
            "project": _profile_read(profile),
            "root_available": False,
            "results": [],
            "error": "No workspace root is available to locate Project roots",
        }
    if not root_path.exists():
        return {
            "project": _profile_read(profile),
            "root_available": False,
            "root_path": str(root_path),
            "results": [],
        }

    relative_roots = _normalise_requested_paths(path, paths)
    results: list[dict[str, Any]] = []
    for file_path in _iter_project_files(root_path, relative_roots, glob):
        result = _project_file_search_result(profile, root_path, file_path, query or "")
        if result is None:
            continue
        results.append(result)
        if len(results) >= limit:
            break
    return {
        "project": _profile_read(profile),
        "root_available": True,
        "root_path": str(root_path),
        "results": results,
    }


def mount_project_reference(
    profile,
    *,
    path: str | None,
    paths: list[str] | None,
    glob: str | None,
    limit: int | None,
    mount_path: str | None,
) -> dict[str, Any]:
    root_path = _profile_root_path(profile)
    if root_path is None:
        raise ValueError("No workspace root is available to locate Project roots")
    if not root_path.exists():
        raise ValueError(f"Project root is not materialized yet: {root_path}")

    if glob and not path and not paths:
        max_matches = limit_value(limit, default=PROJECT_FILE_SEARCH_DEFAULT_LIMIT, maximum=PROJECT_FILE_SEARCH_MAX_LIMIT)
        relative_paths = [
            candidate.relative_to(root_path).as_posix()
            for candidate in _iter_project_files(root_path, [""], glob)
        ][:max_matches]
        if not relative_paths:
            raise ValueError(f"No Project files matched glob: {glob}")
    else:
        relative_paths = _normalise_requested_paths(path, paths)

    workspace_ref = _ensure_workspace_ref_mapping()
    manifest = _ensure_manifest_payload(workspace_ref)
    mounts = manifest["mounts"]
    workspaces = manifest["workspaces"]

    mounted: list[dict[str, Any]] = []
    for relative_path in relative_paths:
        agent_mount_path = _mount_path_for_reference(profile, relative_path, mount_path, count=len(relative_paths))
        mount = _reference_mount_payload(profile, root_path, relative_path, agent_mount_path)
        mounts[:] = [
            existing
            for existing in mounts
            if not (isinstance(existing, dict) and existing.get("mount_path") == agent_mount_path)
        ]
        workspaces[:] = [
            existing
            for existing in workspaces
            if not (isinstance(existing, dict) and existing.get("name") == agent_mount_path)
        ]
        mounts.append(mount)
        workspaces.append({"name": agent_mount_path, "path": mount["workspace_path"]})
        mounted.append(mount)

    _sync_workspace_ref_to_run(workspace_ref)
    return {
        "ok": True,
        "project": _profile_read(profile),
        "mounts": mounted,
        "guidance": (
            "Reference mounts are read-only. Use normal file tools such as read_file, list_files, "
            "or search_files against the returned mount_path values."
        ),
    }


def _profile_read(profile) -> dict[str, Any]:
    return profile_to_read(profile).model_dump(mode="json", by_alias=True)


def _profile_resources(profile) -> list[dict[str, Any]]:
    context = profile.project_context if isinstance(profile.project_context, Mapping) else {}
    return [dict(item) for item in (context.get("resources") or []) if isinstance(item, Mapping)]


def _profile_search_blob(profile) -> str:
    metadata = getattr(profile, "metadata_", None)
    context = getattr(profile, "project_context", None)
    parts: list[str] = []
    for value in (
        getattr(profile, "id", None),
        getattr(profile, "slug", None),
        getattr(profile, "name", None),
        getattr(profile, "description", None),
    ):
        if value is not None:
            parts.append(str(value))
    for payload in (metadata, context):
        if isinstance(payload, Mapping):
            parts.append(json.dumps(payload, sort_keys=True, default=str))
    return " ".join(parts).lower()


def _profile_project_key(profile) -> str:
    context = stamped_project_context(profile)
    return project_key_from_context(
        context,
        resources=_profile_resources(profile),
        fallback=getattr(profile, "slug", None) or getattr(profile, "id", None),
    )


def _current_workspace_root_path() -> Path | None:
    workspace_root = _current_workspace_root_hint() or _patched_workspace_root()
    if not isinstance(workspace_root, str) or not workspace_root.strip():
        return None
    return Path(workspace_root).expanduser()


def _profile_root_path(profile) -> Path | None:
    workspace_root = _current_workspace_root_path()
    if workspace_root is None:
        return None
    return project_root_path(workspace_root, _profile_project_key(profile))


def _safe_project_relative_path(value: Any) -> str | None:
    text = str(value or "").replace("\\", "/").strip()
    if text in {"", ".", "/"}:
        return ""
    text = text.strip("/")
    path = PurePosixPath(text)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path.as_posix()


def _safe_reference_segment(value: Any, *, fallback: str) -> str:
    text = str(value or fallback).strip().strip("/")
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "-" for ch in text)[:80]
    return safe or fallback


def _normalise_requested_paths(path: str | None, paths: list[str] | None) -> list[str]:
    raw_paths: list[Any] = []
    if path is not None:
        raw_paths.append(path)
    if isinstance(paths, list):
        raw_paths.extend(paths)
    if not raw_paths:
        return [""]

    normalised: list[str] = []
    seen: set[str] = set()
    for raw_path in raw_paths:
        relative = _safe_project_relative_path(raw_path)
        if relative is None:
            raise ValueError(f"Invalid Project path: {raw_path}")
        if relative not in seen:
            normalised.append(relative)
            seen.add(relative)
    return normalised or [""]


def _is_internal_project_path(relative_path: Path) -> bool:
    return any(part in _PROJECT_INTERNAL_DIRS for part in relative_path.parts)


def _iter_project_files(root_path: Path, relative_roots: Iterable[str], glob: str | None = None) -> Iterable[Path]:
    for relative_root in relative_roots:
        base_path = root_path / relative_root if relative_root else root_path
        if not base_path.exists():
            continue
        candidates = [base_path] if base_path.is_file() else base_path.rglob("*")
        for candidate in candidates:
            if not candidate.is_file():
                continue
            try:
                relative = candidate.relative_to(root_path)
            except ValueError:
                continue
            if _is_internal_project_path(relative):
                continue
            relative_posix = relative.as_posix()
            if glob and not fnmatch(relative_posix, glob) and not fnmatch(candidate.name, glob):
                continue
            yield candidate


def _file_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}:
        return "image"
    if suffix == ".pdf":
        return "pdf"
    if suffix in {".csv", ".tsv", ".xls", ".xlsx"}:
        return "sheet"
    if suffix in _TEXT_FILE_EXTENSIONS:
        return "text"
    return suffix.lstrip(".") or "file"


def _looks_textual(path: Path, data: bytes) -> bool:
    if path.suffix.lower() in _TEXT_FILE_EXTENSIONS:
        return True
    if b"\x00" in data[:4096]:
        return False
    try:
        data[:4096].decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def _content_snippets(path: Path, query: str, *, max_snippets: int = 3) -> list[dict[str, Any]]:
    terms = _query_terms(query)
    if not terms:
        return []
    try:
        data = path.read_bytes()[:_PROJECT_FILE_SEARCH_MAX_BYTES]
    except OSError:
        return []
    if not _looks_textual(path, data):
        return []
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("utf-8", errors="replace")
    snippets: list[dict[str, Any]] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        lowered = line.lower()
        if all(term in lowered for term in terms):
            snippets.append({"line": line_no, "text": line.strip()[:300]})
            if len(snippets) >= max_snippets:
                break
    return snippets


def _project_file_search_result(profile, root_path: Path, file_path: Path, query: str) -> dict[str, Any] | None:
    relative_path = file_path.relative_to(root_path).as_posix()
    path_blob = relative_path.lower()
    terms = _query_terms(query)
    path_matches = bool(terms and all(term in path_blob for term in terms))
    snippets = _content_snippets(file_path, query)
    if terms and not path_matches and not snippets:
        return None
    stat = file_path.stat()
    project_segment = _safe_reference_segment(getattr(profile, "slug", None) or getattr(profile, "id", None), fallback="project")
    return {
        "project_id": getattr(profile, "id", None),
        "project_slug": getattr(profile, "slug", None),
        "project_name": getattr(profile, "name", None),
        "path": relative_path,
        "mount_path_hint": f"{_PROJECT_REFERENCE_MOUNT_ROOT}/{project_segment}/{relative_path}",
        "kind": _file_kind(file_path),
        "size_bytes": stat.st_size,
        "matched_by": ["path"] if path_matches else ["content"],
        "snippets": snippets,
    }


def _ensure_workspace_ref_mapping() -> dict[str, Any]:
    workspace_ref = getattr(_agent_context, "workspace_ref", None)
    if not isinstance(workspace_ref, dict):
        run = getattr(_agent_context, "run", None)
        workspace_ref = getattr(run, "workspace_ref", None)
    if not isinstance(workspace_ref, dict):
        workspace_ref = {}
    setattr(_agent_context, "workspace_ref", workspace_ref)
    return workspace_ref


def _ensure_manifest_payload(workspace_ref: dict[str, Any]) -> dict[str, Any]:
    manifest = workspace_ref.get("project_workspace_manifest")
    if not isinstance(manifest, dict):
        manifest = {}
        workspace_ref["project_workspace_manifest"] = manifest
    mounts = manifest.get("mounts")
    if not isinstance(mounts, list):
        manifest["mounts"] = []
    workspaces = manifest.get("workspaces")
    if not isinstance(workspaces, list):
        manifest["workspaces"] = []
    if not manifest.get("schema_version"):
        manifest["schema_version"] = 1
    return manifest


def _sync_workspace_ref_to_run(workspace_ref: dict[str, Any]) -> None:
    run = getattr(_agent_context, "run", None)
    if run is None:
        return
    try:
        run.workspace_ref = workspace_ref
    except Exception:
        return
    metadata = getattr(run, "metadata_", None)
    if isinstance(metadata, dict):
        metadata["project_workspace_manifest"] = workspace_ref.get("project_workspace_manifest")


def _mount_path_for_reference(profile, relative_path: str, mount_path: str | None, *, count: int) -> str:
    if mount_path:
        safe_mount_path = _safe_project_relative_path(mount_path)
        if safe_mount_path is None:
            raise ValueError(f"Invalid reference mount_path: {mount_path}")
        base = "/" + safe_mount_path.strip("/")
    else:
        segment = _safe_reference_segment(getattr(profile, "slug", None) or getattr(profile, "id", None), fallback="project")
        base = f"{_PROJECT_REFERENCE_MOUNT_ROOT}/{segment}"
    if count <= 1 and not relative_path:
        return base
    suffix = relative_path if relative_path else "root"
    return f"{base.rstrip('/')}/{suffix}"


def _reference_mount_payload(profile, root_path: Path, relative_path: str, mount_path: str) -> dict[str, Any]:
    source_path = root_path / relative_path if relative_path else root_path
    if not source_path.exists():
        raise ValueError(f"Project path does not exist: {relative_path or '/'}")
    metadata = {
        "reference_mount": True,
        "read_only": True,
        "source_project_id": getattr(profile, "id", None),
        "source_project_slug": getattr(profile, "slug", None),
        "source_project_name": getattr(profile, "name", None),
        "source_relative_path": relative_path,
        "materialization": {
            "provider": "project_reference",
            "workspace_path": str(source_path),
            "path": str(source_path),
            "source_path": str(source_path),
            "read_only": True,
            "reference_mount": True,
        },
    }
    return {
        "id": mount_path,
        "resource_id": f"reference:{getattr(profile, 'id', 'project')}:{relative_path or '/'}",
        "kind": "folder" if source_path.is_dir() else "file",
        "mount_path": mount_path,
        "workspace_path": str(source_path),
        "resource_path": str(source_path),
        "source_path": str(source_path),
        "label": f"{getattr(profile, 'name', None) or getattr(profile, 'slug', None) or 'Project'}: {relative_path or '/'}",
        "metadata": metadata,
        "file_kind": _file_kind(source_path) if source_path.is_file() else "folder",
    }


__all__ = [
    "PROJECT_FILE_SEARCH_DEFAULT_LIMIT",
    "PROJECT_FILE_SEARCH_MAX_LIMIT",
    "limit_value",
    "mount_project_reference",
    "profile_matches_query",
    "search_project_root_files",
]
