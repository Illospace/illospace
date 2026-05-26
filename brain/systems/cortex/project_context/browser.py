"""Project draft browser payloads for the thread UI."""
from __future__ import annotations

from collections.abc import Mapping
import mimetypes
import os
from pathlib import Path, PurePosixPath
from typing import Any

from brain.systems.cortex.project_context.drafts import (
    DRAFT_METADATA_DIR,
    build_file_manifest,
    load_draft_metadata,
)


MAX_BROWSER_FILES = 2_000
MAX_FILE_PREVIEW_BYTES = 120_000
MAX_FILE_UPDATE_BYTES = 2_000_000
_TEXT_EXTENSIONS = {
    ".css",
    ".csv",
    ".html",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".mjs",
    ".py",
    ".rst",
    ".svelte",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _root_path(value: Any) -> Path | None:
    text = _clean_text(value)
    if not text:
        return None
    return Path(text).expanduser()


def _safe_relative_path(value: Any) -> str:
    text = _clean_text(value).replace("\\", "/").lstrip("/")
    pure = PurePosixPath(text)
    if not text or pure.is_absolute() or ".." in pure.parts:
        raise ValueError("Project file path must stay inside the mounted Project resource.")
    if pure.parts and pure.parts[0].startswith(".illo-project-"):
        raise ValueError("Project internal metadata is not browsable.")
    return pure.as_posix()


def _manifest_for(root: Path | None) -> dict[str, dict[str, Any]]:
    if root is None or not root.exists():
        return {}
    return build_file_manifest(root)


def _base_manifest_for(draft_root: Path | None) -> dict[str, dict[str, Any]]:
    if draft_root is None:
        return {}
    metadata = load_draft_metadata(draft_root)
    base = metadata.get("base_manifest")
    if not isinstance(base, Mapping):
        return {}
    return {str(path): dict(entry) for path, entry in base.items() if isinstance(entry, Mapping)}


def _path_set(changes: Mapping[str, Any], key: str) -> set[str]:
    values = changes.get(key)
    if not isinstance(values, list):
        return set()
    paths: set[str] = set()
    for value in values:
        if isinstance(value, str):
            path = value.strip()
        elif isinstance(value, Mapping):
            path = _clean_text(value.get("path") or value.get("relative_path") or value.get("name"))
        else:
            path = ""
        if path:
            paths.add(path)
    return paths


def _entry_status(
    path: str,
    *,
    root_entry: Mapping[str, Any] | None,
    draft_entry: Mapping[str, Any] | None,
    base_entry: Mapping[str, Any] | None,
    changes: Mapping[str, Any],
) -> str:
    if path in _path_set(changes, "conflicted_paths"):
        return "conflicted"
    if path in _path_set(changes, "deleted_paths"):
        return "deleted"
    if path in _path_set(changes, "new_paths"):
        return "new"
    if path in _path_set(changes, "changed_paths"):
        return "changed"
    if path in _path_set(changes, "out_of_date_paths"):
        return "out_of_date"
    if draft_entry is None and base_entry is not None:
        return "deleted"
    if draft_entry is not None and root_entry is None and base_entry is None:
        return "new"
    if draft_entry is not None and base_entry is not None and dict(draft_entry) != dict(base_entry):
        return "changed"
    if draft_entry is not None and root_entry is not None and dict(draft_entry) != dict(root_entry):
        return "changed"
    if path in _path_set(changes, "out_of_date_paths"):
        return "out_of_date"
    return "clean"


def _file_name(path: str) -> str:
    return PurePosixPath(path).name or path


def _file_parent(path: str) -> str:
    parent = PurePosixPath(path).parent.as_posix()
    return "" if parent == "." else parent


def project_resource_file_browser(
    resource: Mapping[str, Any],
    *,
    max_files: int = MAX_BROWSER_FILES,
) -> dict[str, Any]:
    """Return a bounded file list for one materialized Project resource."""

    root = _root_path(resource.get("source_path"))
    draft = _root_path(resource.get("workspace_path") or resource.get("resource_path"))
    root_manifest = _manifest_for(root)
    draft_manifest = _manifest_for(draft)
    base_manifest = _base_manifest_for(draft)
    changes = _as_mapping(resource.get("changes"))
    change_paths = set().union(*(
        _path_set(changes, key)
        for key in ("changed_paths", "new_paths", "deleted_paths", "conflicted_paths", "out_of_date_paths")
    ))
    all_paths = sorted(set(root_manifest) | set(draft_manifest) | set(base_manifest) | change_paths)
    visible_paths = all_paths[:max_files]
    entries: list[dict[str, Any]] = []

    for path in visible_paths:
        root_entry = root_manifest.get(path)
        draft_entry = draft_manifest.get(path)
        base_entry = base_manifest.get(path)
        status = _entry_status(
            path,
            root_entry=root_entry,
            draft_entry=draft_entry,
            base_entry=base_entry,
            changes=changes,
        )
        size = (draft_entry or root_entry or base_entry or {}).get("size")
        entries.append({
            "path": path,
            "name": _file_name(path),
            "parent": _file_parent(path),
            "extension": PurePosixPath(path).suffix.lower(),
            "status": status,
            "layer": "draft" if draft_entry is not None else "root",
            "has_root": root_entry is not None,
            "has_draft": draft_entry is not None,
            "has_base": base_entry is not None,
            "size": size,
            "root_size": root_entry.get("size") if root_entry else None,
            "draft_size": draft_entry.get("size") if draft_entry else None,
            "root_sha256": root_entry.get("sha256") if root_entry else None,
            "draft_sha256": draft_entry.get("sha256") if draft_entry else None,
            "conflicted": status == "conflicted",
            "out_of_date": status == "out_of_date",
        })

    return {
        "entries": entries,
        "summary": {
            "file_count": len(all_paths),
            "visible_count": len(entries),
            "truncated": max(0, len(all_paths) - len(entries)),
        },
    }


def with_project_file_browser(draft_status: Mapping[str, Any]) -> dict[str, Any]:
    """Attach read-only browser entries to a draft-status payload."""

    payload = dict(draft_status)
    resources = payload.get("resources")
    if not isinstance(resources, list):
        payload["file_browser"] = {"entries": [], "summary": {"file_count": 0, "visible_count": 0, "truncated": 0}}
        return payload

    next_resources: list[dict[str, Any]] = []
    aggregate_entries: list[dict[str, Any]] = []
    total_files = 0
    total_truncated = 0
    for resource in resources:
        if not isinstance(resource, Mapping):
            continue
        item = dict(resource)
        browser = project_resource_file_browser(item)
        item["file_browser"] = browser
        summary = _as_mapping(browser.get("summary"))
        total_files += int(summary.get("file_count") or 0)
        total_truncated += int(summary.get("truncated") or 0)
        for entry in browser.get("entries") or []:
            if isinstance(entry, Mapping):
                aggregate_entries.append({
                    **dict(entry),
                    "resource_id": item.get("id"),
                    "mount_path": item.get("mount_path"),
                    "resource_label": item.get("label"),
                })
        next_resources.append(item)

    payload["resources"] = next_resources
    payload["file_browser"] = {
        "entries": aggregate_entries,
        "summary": {
            "file_count": total_files,
            "visible_count": len(aggregate_entries),
            "truncated": total_truncated,
        },
    }
    return payload


def _resolve_file_path(root: Path | None, relative_path: str) -> Path | None:
    if root is None:
        return None
    root = root.expanduser()
    if root.is_file():
        if relative_path != root.name:
            return None
        return root
    return root / relative_path


def _writable_draft_file(root: Path | None, relative_path: str) -> Path:
    if root is None:
        raise ValueError("Project draft workspace is not writable for this file.")
    root = root.expanduser()
    if root.is_file():
        if relative_path != root.name:
            raise ValueError("Project file path is outside the draft resource.")
        if root.is_symlink():
            raise ValueError("Project draft file cannot be a symlink.")
        return root

    target = root / relative_path
    if target.exists() and target.is_symlink():
        raise ValueError("Project draft file cannot be a symlink.")
    if target.exists() and not target.is_file():
        raise ValueError("Project draft path is not a regular file.")
    target.parent.mkdir(parents=True, exist_ok=True)
    root_resolved = root.resolve()
    parent_resolved = target.parent.resolve()
    if os.path.commonpath([str(root_resolved), str(parent_resolved)]) != str(root_resolved):
        raise ValueError("Project file path must stay inside the mounted Project resource.")
    return target


def _read_text_layer(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {"exists": False}
    try:
        if not path.exists() or not path.is_file() or path.is_symlink():
            return {"exists": False}
        size = path.stat().st_size
        with path.open("rb") as handle:
            raw = handle.read(MAX_FILE_PREVIEW_BYTES + 1)
        truncated = len(raw) > MAX_FILE_PREVIEW_BYTES
        raw = raw[:MAX_FILE_PREVIEW_BYTES]
        suffix = path.suffix.lower()
        is_probably_text = suffix in _TEXT_EXTENSIONS or b"\x00" not in raw[:4096]
        if not is_probably_text:
            return {"exists": True, "binary": True, "size": size, "truncated": False}
        return {
            "exists": True,
            "binary": False,
            "size": size,
            "content": raw.decode("utf-8", errors="replace"),
            "truncated": truncated,
        }
    except OSError as exc:
        return {"exists": False, "error": str(exc)}


def project_file_payload(
    draft_status: Mapping[str, Any],
    *,
    resource_id: str | None,
    path: str,
) -> dict[str, Any]:
    """Return bounded root/base/draft contents for a selected Project file."""

    relative_path = _safe_relative_path(path)
    resources = [resource for resource in draft_status.get("resources") or [] if isinstance(resource, Mapping)]
    resource = _select_resource(resources, resource_id, relative_path)
    if resource is None:
        raise ValueError("Project resource not found for selected file.")

    root = _root_path(resource.get("source_path"))
    draft = _root_path(resource.get("workspace_path") or resource.get("resource_path"))
    root_file = _resolve_file_path(root, relative_path)
    draft_file = _resolve_file_path(draft, relative_path)
    base_file = draft / DRAFT_METADATA_DIR / "base" / relative_path if draft is not None else None
    entry = project_resource_file_browser(resource, max_files=MAX_BROWSER_FILES)
    selected_entry = next(
        (dict(item) for item in entry.get("entries") or [] if isinstance(item, Mapping) and item.get("path") == relative_path),
        {"path": relative_path, "name": _file_name(relative_path), "status": "unknown"},
    )
    return {
        "ok": True,
        "resource_id": resource.get("id"),
        "mount_path": resource.get("mount_path"),
        "path": relative_path,
        "entry": selected_entry,
        "layers": {
            "root": _read_text_layer(root_file),
            "base": _read_text_layer(base_file),
            "draft": _read_text_layer(draft_file),
        },
    }


def project_file_blob(
    draft_status: Mapping[str, Any],
    *,
    resource_id: str | None,
    path: str,
    layer: str,
) -> dict[str, Any]:
    """Resolve a selected root/base/draft file for browser-native previews."""

    relative_path = _safe_relative_path(path)
    layer_key = _clean_text(layer).lower() or "draft"
    if layer_key not in {"root", "base", "draft"}:
        raise ValueError("Project file layer must be root, base, or draft.")

    resources = [resource for resource in draft_status.get("resources") or [] if isinstance(resource, Mapping)]
    resource = _select_resource(resources, resource_id, relative_path)
    if resource is None:
        raise ValueError("Project resource not found for selected file.")

    root = _root_path(resource.get("source_path"))
    draft = _root_path(resource.get("workspace_path") or resource.get("resource_path"))
    if layer_key == "root":
        file_path = _resolve_file_path(root, relative_path)
    elif layer_key == "base":
        file_path = draft / DRAFT_METADATA_DIR / "base" / relative_path if draft is not None else None
    else:
        file_path = _resolve_file_path(draft, relative_path)

    if file_path is None or not file_path.exists() or not file_path.is_file() or file_path.is_symlink():
        raise ValueError("Project file layer is not available for preview.")
    media_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    return {
        "path": file_path,
        "filename": file_path.name,
        "media_type": media_type,
    }


def update_project_draft_file(
    draft_status: Mapping[str, Any],
    *,
    resource_id: str | None,
    path: str,
    content: str,
) -> dict[str, Any]:
    """Write a text file into the thread draft overlay and return its refreshed preview."""

    relative_path = _safe_relative_path(path)
    encoded = content.encode("utf-8")
    if len(encoded) > MAX_FILE_UPDATE_BYTES:
        raise ValueError("Project draft file update is too large.")

    resources = [resource for resource in draft_status.get("resources") or [] if isinstance(resource, Mapping)]
    resource = _select_resource(resources, resource_id, relative_path)
    if resource is None:
        raise ValueError("Project resource not found for selected file.")

    draft = _root_path(resource.get("workspace_path") or resource.get("resource_path"))
    target = _writable_draft_file(draft, relative_path)
    try:
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"Project draft file could not be updated: {exc}") from exc

    return {
        **project_file_payload(draft_status, resource_id=resource_id, path=relative_path),
        "updated": True,
    }


def _select_resource(
    resources: list[Mapping[str, Any]],
    resource_id: str | None,
    relative_path: str,
) -> Mapping[str, Any] | None:
    if resource_id:
        wanted = resource_id.strip()
        for resource in resources:
            candidates = {
                _clean_text(resource.get("id")),
                _clean_text(resource.get("mount_path")),
                _clean_text(resource.get("label")),
            }
            if wanted in candidates:
                return resource
    if len(resources) == 1:
        return resources[0]
    for resource in resources:
        browser = project_resource_file_browser(resource)
        if any(isinstance(item, Mapping) and item.get("path") == relative_path for item in browser.get("entries") or []):
            return resource
    return None


__all__ = [
    "project_file_payload",
    "project_resource_file_browser",
    "with_project_file_browser",
]
