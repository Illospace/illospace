"""Illo-managed local Project root version history."""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any
import hashlib
import json
import os
import re
import shutil


HISTORY_DIR = ".illo-project-history"
VERSION_SCHEMA_VERSION = 1
VERSION_FILE = "version.json"
INDEX_FILE = "index.json"

_IGNORED_MANIFEST_DIRS = {HISTORY_DIR, ".illo-project-draft"}

FileManifest = dict[str, dict[str, Any]]


@dataclass(frozen=True)
class ProjectRootVersion:
    version_id: str
    root: Path
    root_kind: str
    label: str
    created_at: str
    manifest: FileManifest
    store_path: Path
    files_path: Path
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: int = VERSION_SCHEMA_VERSION

    @property
    def id(self) -> str:
        return self.version_id

    @property
    def paths(self) -> list[str]:
        return sorted(self.manifest)

    @property
    def file_count(self) -> int:
        return sum(1 for entry in self.manifest.values() if entry.get("kind") == "file")

    @property
    def directory_count(self) -> int:
        return sum(1 for entry in self.manifest.values() if entry.get("kind") == "directory")

    @property
    def total_size(self) -> int:
        return sum(int(entry.get("size") or 0) for entry in self.manifest.values())

    def to_summary_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.version_id,
            "version_id": self.version_id,
            "root": str(self.root),
            "root_kind": self.root_kind,
            "label": self.label,
            "created_at": self.created_at,
            "metadata": self.metadata,
            "file_count": self.file_count,
            "directory_count": self.directory_count,
            "total_size": self.total_size,
            "paths": self.paths,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "id": self.version_id,
            "version_id": self.version_id,
            "root": str(self.root),
            "root_kind": self.root_kind,
            "label": self.label,
            "created_at": self.created_at,
            "manifest": self.manifest,
            "metadata": self.metadata,
            "file_count": self.file_count,
            "directory_count": self.directory_count,
            "total_size": self.total_size,
            "paths": self.paths,
            "store_path": str(self.store_path),
            "files_path": str(self.files_path),
        }


@dataclass(frozen=True)
class ProjectRootVersionComparison:
    root: Path
    version_id: str
    root_kind: str
    label: str
    created_at: str
    current_manifest: FileManifest
    version_manifest: FileManifest
    created: list[str]
    modified: list[str]
    deleted: list[str]
    current_only: list[str]
    version_only: list[str]
    unchanged: list[str]
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: int = VERSION_SCHEMA_VERSION

    @property
    def has_changes(self) -> bool:
        return bool(self.created or self.modified or self.deleted)

    @property
    def changed_paths(self) -> list[str]:
        return sorted({*self.created, *self.modified, *self.deleted})

    @property
    def summary(self) -> dict[str, Any]:
        return {
            "has_changes": self.has_changes,
            "created_count": len(self.created),
            "modified_count": len(self.modified),
            "deleted_count": len(self.deleted),
            "current_only_count": len(self.current_only),
            "version_only_count": len(self.version_only),
            "unchanged_count": len(self.unchanged),
            "current_file_count": len(self.current_manifest),
            "version_file_count": len(self.version_manifest),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "root": str(self.root),
            "root_kind": self.root_kind,
            "version_id": self.version_id,
            "label": self.label,
            "created_at": self.created_at,
            "metadata": self.metadata,
            "created": self.created,
            "modified": self.modified,
            "deleted": self.deleted,
            "current_only": self.current_only,
            "version_only": self.version_only,
            "unchanged": self.unchanged,
            "changed_paths": self.changed_paths,
            "summary": self.summary,
        }


def capture_project_root_version(
    root: Path,
    *,
    label: str,
    metadata: Mapping[str, Any] | None = None,
) -> ProjectRootVersion:
    """Capture the current local Project root into Illo's hidden history store."""

    root = Path(root).expanduser()
    root_kind = _root_kind_for_capture(root)
    history_dir = _history_dir_for_root(root, root_kind)
    versions_dir = history_dir / "versions"
    versions_dir.mkdir(parents=True, exist_ok=True)

    manifest = build_project_root_manifest(root)
    created_at = _utc_timestamp()
    version_id = _unique_version_id(versions_dir, created_at, label, manifest)
    tmp_dir = versions_dir / f".tmp-{version_id}"
    version_dir = versions_dir / version_id

    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True)
    files_dir = tmp_dir / "files"
    files_dir.mkdir()

    try:
        for relative_path, entry in manifest.items():
            if entry.get("kind") == "directory":
                _ensure_directory(files_dir / _safe_relative_path(relative_path))
                continue
            source = _source_path_for_manifest(root, root_kind, relative_path)
            destination = files_dir / _safe_relative_path(relative_path)
            _ensure_directory(destination.parent)
            shutil.copy2(source, destination)

        payload = {
            "schema_version": VERSION_SCHEMA_VERSION,
            "id": version_id,
            "root_kind": root_kind,
            "root_path": str(root),
            "label": str(label),
            "created_at": created_at,
            "manifest": manifest,
            "metadata": _json_safe(metadata or {}),
        }
        _write_json(tmp_dir / VERSION_FILE, payload)
        tmp_dir.rename(version_dir)
    except Exception:
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        raise

    version = _version_from_payload(root, version_dir, payload)
    _write_history_index(history_dir, root)
    return version


def list_project_root_versions(root: Path) -> list[ProjectRootVersion]:
    """Return captured versions for ``root`` oldest-first."""

    root = Path(root).expanduser()
    history_dir = _history_dir_for_existing_root(root)
    if history_dir is None:
        return []
    versions_dir = history_dir / "versions"
    if not versions_dir.exists():
        return []

    versions: list[ProjectRootVersion] = []
    for version_dir in sorted(path for path in versions_dir.iterdir() if path.is_dir()):
        if version_dir.name.startswith(".tmp-"):
            continue
        payload = _read_version_payload(version_dir)
        if payload is None:
            continue
        versions.append(_version_from_payload(root, version_dir, payload))
    return sorted(versions, key=lambda version: (version.created_at, version.version_id))


def restore_project_root_version(root: Path, version_id: str) -> ProjectRootVersion:
    """Restore regular files in ``root`` to a captured Project root version."""

    root = Path(root).expanduser()
    version = _find_project_root_version(root, version_id)
    if version.root_kind == "folder":
        _restore_folder_root(root, version)
    elif version.root_kind == "file":
        _restore_file_root(root, version)
    else:
        raise ValueError(f"Unsupported Project root kind: {version.root_kind}")
    return version


def compare_project_root_to_version(root: Path, version_id: str) -> ProjectRootVersionComparison:
    """Preview how restoring ``root`` to ``version_id`` would change Project files."""

    root = Path(root).expanduser()
    version = _find_project_root_version(root, version_id)
    current_manifest = build_project_root_manifest(root)
    version_manifest = version.manifest

    current_paths = set(current_manifest)
    version_paths = set(version_manifest)
    current_only = sorted(current_paths - version_paths)
    version_only = sorted(version_paths - current_paths)
    common_paths = current_paths & version_paths
    modified = sorted(
        path
        for path in common_paths
        if not _manifest_entries_match(current_manifest[path], version_manifest[path])
    )
    unchanged = sorted(
        path
        for path in common_paths
        if _manifest_entries_match(current_manifest[path], version_manifest[path])
    )

    return ProjectRootVersionComparison(
        root=root,
        version_id=version.version_id,
        root_kind=version.root_kind,
        label=version.label,
        created_at=version.created_at,
        current_manifest=current_manifest,
        version_manifest=version_manifest,
        created=version_only,
        modified=modified,
        deleted=current_only,
        current_only=current_only,
        version_only=version_only,
        unchanged=unchanged,
        metadata=dict(version.metadata),
        schema_version=version.schema_version,
    )


def build_project_root_manifest(root: Path) -> FileManifest:
    """Build a deterministic manifest of Project files and folders, excluding Illo history."""

    root = Path(root).expanduser()
    if not root.exists():
        return {}
    manifest: FileManifest = {}
    for relative_path, file_path in _iter_manifest_files(root):
        manifest[relative_path] = (
            _directory_manifest_entry()
            if file_path.is_dir() and not file_path.is_symlink()
            else _file_manifest_entry(file_path)
        )
    return dict(sorted(manifest.items()))


def summarize_versions(root_or_versions: Path | str | Sequence[ProjectRootVersion]) -> dict[str, Any]:
    """Return compact version history metadata for a Project root or version sequence."""

    if isinstance(root_or_versions, str | os.PathLike):
        versions = list_project_root_versions(Path(root_or_versions).expanduser())
    else:
        versions = list(root_or_versions)

    latest = versions[-1] if versions else None
    return {
        "schema_version": VERSION_SCHEMA_VERSION,
        "summary": {
            "version_count": len(versions),
            "latest_version_id": latest.version_id if latest else None,
            "latest_created_at": latest.created_at if latest else None,
            "root": str(latest.root) if latest else None,
            "root_kind": latest.root_kind if latest else None,
            "total_file_count": sum(version.file_count for version in versions),
            "total_size": sum(version.total_size for version in versions),
        },
        "versions": [version.to_summary_dict() for version in versions],
    }


def build_project_root_version_metadata(
    *,
    run_id: Any = None,
    idea_id: Any = None,
    actor_id: Any = None,
    org_id: Any = None,
    resource_id: Any = None,
    mount_path: Any = None,
    phase: Any = None,
    operations: Sequence[Mapping[str, Any]] | None = None,
    event_type: Any = "project_root_publish",
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build JSON-safe metadata for Project root versions captured around publish flows."""

    operation_list = [dict(operation) for operation in operations or [] if isinstance(operation, Mapping)]
    payload: dict[str, Any] = {
        "run_id": _clean_metadata_text(run_id),
        "idea_id": _clean_metadata_text(idea_id),
        "resource_id": _clean_metadata_text(resource_id),
        "mount_path": _clean_metadata_text(mount_path),
        "phase": _clean_metadata_text(phase),
        "operation_count": len(operation_list),
    }
    operation_kinds = _unique_metadata_values(
        operation.get("operation") or operation.get("kind") for operation in operation_list
    )
    operation_paths = _unique_metadata_values(
        operation.get(key)
        for operation in operation_list
        for key in ("path", "target_path", "draft_path")
    )
    operation_summary = _operation_summary(operation_list)
    publish_event = {
        "type": _clean_metadata_text(event_type) or "project_root_publish",
        "agent_run_id": payload["run_id"],
        "thread_id": payload["idea_id"],
        "actor_id": _clean_metadata_text(actor_id),
        "org_id": _clean_metadata_text(org_id),
        "resource_id": payload["resource_id"],
        "mount_path": payload["mount_path"],
        "phase": payload["phase"],
        "operation_count": len(operation_list),
        "operation_kinds": operation_kinds,
    }
    if operation_kinds:
        payload["operation_kinds"] = operation_kinds
    if operation_paths:
        payload["operation_paths"] = operation_paths
    payload["operation_summary"] = operation_summary
    payload["diff_summary"] = operation_summary
    payload["publish_event"] = {key: value for key, value in publish_event.items() if value not in (None, "", [], {})}
    if extra:
        payload.update(dict(extra))
    return _json_safe(payload)


def _root_kind_for_capture(root: Path) -> str:
    if root.is_symlink():
        raise ValueError(f"Project root may not be a symlink: {root}")
    if root.is_file():
        return "file"
    if root.is_dir():
        return "folder"
    raise FileNotFoundError(f"Project root does not exist: {root}")


def _history_dir_for_root(root: Path, root_kind: str) -> Path:
    if root_kind == "folder":
        return root / HISTORY_DIR
    if root_kind == "file":
        return root.parent / HISTORY_DIR / _single_file_history_key(root)
    raise ValueError(f"Unsupported Project root kind: {root_kind}")


def _history_dir_for_existing_root(root: Path) -> Path | None:
    if root.exists():
        return _history_dir_for_root(root, _root_kind_for_capture(root))

    file_history_dir = _history_dir_for_root(root, "file")
    if file_history_dir.exists():
        return file_history_dir
    folder_history_dir = _history_dir_for_root(root, "folder")
    if folder_history_dir.exists():
        return folder_history_dir
    return None


def _single_file_history_key(root: Path) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", root.stem).strip(".-")
    return stem or hashlib.sha256(root.name.encode("utf-8")).hexdigest()[:12]


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _unique_version_id(versions_dir: Path, created_at: str, label: str, manifest: FileManifest) -> str:
    timestamp = created_at.replace("-", "").replace(":", "").replace(".", "")
    timestamp = timestamp.removesuffix("Z") + "Z"
    label_slug = re.sub(r"[^A-Za-z0-9._-]+", "-", label.lower()).strip(".-")[:48] or "version"
    digest = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:12]
    base = f"{timestamp}-{label_slug}-{digest}"
    candidate = base
    counter = 2
    while (versions_dir / candidate).exists() or (versions_dir / f".tmp-{candidate}").exists():
        candidate = f"{base}-{counter}"
        counter += 1
    return candidate


def _is_ignored_manifest_relative(relative_path: Path | PurePosixPath) -> bool:
    return any(part in _IGNORED_MANIFEST_DIRS for part in relative_path.parts)


def _iter_manifest_files(root: Path) -> list[tuple[str, Path]]:
    if root.is_file() and not root.is_symlink():
        return [(root.name, root)]
    if not root.is_dir():
        return []

    paths: list[tuple[str, Path]] = []
    for current, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in _IGNORED_MANIFEST_DIRS)
        current_path = Path(current)
        for dirname in dirnames:
            path = current_path / dirname
            if path.is_symlink():
                continue
            relative = path.relative_to(root)
            if _is_ignored_manifest_relative(relative):
                continue
            paths.append((relative.as_posix(), path))
        for filename in sorted(filenames):
            path = current_path / filename
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(root)
            if _is_ignored_manifest_relative(relative):
                continue
            paths.append((relative.as_posix(), path))
    return sorted(paths)


def _file_manifest_entry(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "kind": "file",
        "sha256": digest.hexdigest(),
        "size": path.stat().st_size,
    }


def _directory_manifest_entry() -> dict[str, Any]:
    return {"kind": "directory"}


def _manifest_entries_match(current: Mapping[str, Any], version: Mapping[str, Any]) -> bool:
    if current.get("sha256") is not None or version.get("sha256") is not None:
        return (
            current.get("kind") == version.get("kind")
            and current.get("sha256") == version.get("sha256")
            and current.get("size") == version.get("size")
        )
    return dict(current) == dict(version)


def _clean_metadata_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _unique_metadata_values(values: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        text = _clean_metadata_text(value)
        if text is None or text in seen:
            continue
        seen.add(text)
        unique.append(text)
    return unique


def _operation_summary(operations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    paths: set[str] = set()
    for operation in operations:
        kind = _clean_metadata_text(operation.get("operation") or operation.get("kind")) or "unknown"
        counts[kind] = counts.get(kind, 0) + 1
        path = _clean_metadata_text(operation.get("path"))
        if path:
            paths.add(path)
    return {
        "operation_count": len(operations),
        "path_count": len(paths),
        "paths": sorted(paths),
        "by_operation": dict(sorted(counts.items())),
    }


def _source_path_for_manifest(root: Path, root_kind: str, relative_path: str) -> Path:
    if root_kind == "file":
        expected = PurePosixPath(root.name)
        if PurePosixPath(relative_path) != expected:
            raise ValueError(f"File Project root cannot contain manifest path: {relative_path}")
        return root
    return root / _safe_relative_path(relative_path)


def _safe_relative_path(relative_path: str) -> Path:
    pure = PurePosixPath(relative_path)
    if pure.is_absolute() or not pure.parts:
        raise ValueError(f"Unsafe Project version path: {relative_path}")
    if any(part in {"", ".", ".."} for part in pure.parts):
        raise ValueError(f"Unsafe Project version path: {relative_path}")
    if _is_ignored_manifest_relative(pure):
        raise ValueError(f"Project version path targets hidden Illo metadata: {relative_path}")
    return Path(*pure.parts)


def _ensure_directory(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        return
    if path.exists() or path.is_symlink():
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    if not path.parent.exists():
        _ensure_directory(path.parent)
    path.mkdir()


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    tmp_path.replace(path)


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, tuple | list):
        return [_json_safe(item) for item in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


def _read_version_payload(version_dir: Path) -> dict[str, Any] | None:
    version_file = version_dir / VERSION_FILE
    if not version_file.exists():
        return None
    try:
        with version_file.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _version_from_payload(root: Path, version_dir: Path, payload: Mapping[str, Any]) -> ProjectRootVersion:
    return ProjectRootVersion(
        version_id=str(payload.get("id") or version_dir.name),
        root=root,
        root_kind=str(payload.get("root_kind") or "folder"),
        label=str(payload.get("label") or ""),
        created_at=str(payload.get("created_at") or ""),
        manifest=_normalise_manifest(payload.get("manifest")),
        store_path=version_dir,
        files_path=version_dir / "files",
        metadata=dict(payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}),
        schema_version=int(payload.get("schema_version") or VERSION_SCHEMA_VERSION),
    )


def _normalise_manifest(value: Any) -> FileManifest:
    if not isinstance(value, Mapping):
        return {}
    manifest: FileManifest = {}
    for raw_path, raw_entry in value.items():
        if not isinstance(raw_path, str) or not isinstance(raw_entry, Mapping):
            continue
        relative = PurePosixPath(raw_path)
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            continue
        if _is_ignored_manifest_relative(relative):
            continue
        manifest[relative.as_posix()] = dict(raw_entry)
    return dict(sorted(manifest.items()))


def _write_history_index(history_dir: Path, root: Path) -> None:
    versions = list_project_root_versions(root)
    summary = summarize_versions(versions)["summary"]
    payload = {
        "schema_version": VERSION_SCHEMA_VERSION,
        "summary": summary,
        "versions": [version.to_dict() for version in versions],
    }
    _write_json(history_dir / INDEX_FILE, payload)


def _find_project_root_version(root: Path, version_id: str) -> ProjectRootVersion:
    for version in list_project_root_versions(root):
        if version.version_id == version_id:
            return version
    raise FileNotFoundError(f"Project root version not found: {version_id}")


def _archive_path(version: ProjectRootVersion, relative_path: str) -> Path:
    path = version.files_path / _safe_relative_path(relative_path)
    if not path.is_file():
        raise FileNotFoundError(f"Project root version file is missing: {relative_path}")
    return path


def _restore_folder_root(root: Path, version: ProjectRootVersion) -> None:
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        root.unlink()
    root.mkdir(parents=True, exist_ok=True)

    target_paths = set(version.manifest)
    for relative_path, current_path in _iter_manifest_files(root):
        if relative_path not in target_paths:
            if current_path.is_dir() and not current_path.is_symlink():
                shutil.rmtree(current_path)
            else:
                current_path.unlink()

    for relative_path, entry in sorted(version.manifest.items()):
        target = root / _safe_relative_path(relative_path)
        if entry.get("kind") == "directory":
            _ensure_directory(target)
            continue
        source = _archive_path(version, relative_path)
        _copy_restore_file(source, target)


def _restore_file_root(root: Path, version: ProjectRootVersion) -> None:
    expected_path = root.name
    if expected_path not in version.manifest:
        if root.exists() or root.is_symlink():
            if root.is_dir() and not root.is_symlink():
                shutil.rmtree(root)
            else:
                root.unlink()
        return
    _copy_restore_file(_archive_path(version, expected_path), root)


def _copy_restore_file(source: Path, target: Path) -> None:
    _ensure_directory(target.parent)
    if target.exists() or target.is_symlink():
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
    shutil.copy2(source, target)


def _prune_empty_dirs(root: Path) -> None:
    directories = [path for path in root.rglob("*") if path.is_dir() and not path.is_symlink()]
    for path in sorted(directories, key=lambda candidate: len(candidate.parts), reverse=True):
        relative = path.relative_to(root)
        if _is_ignored_manifest_relative(relative):
            continue
        try:
            path.rmdir()
        except OSError:
            continue


__all__ = [
    "FileManifest",
    "HISTORY_DIR",
    "ProjectRootVersionComparison",
    "ProjectRootVersion",
    "build_project_root_version_metadata",
    "build_project_root_manifest",
    "capture_project_root_version",
    "compare_project_root_to_version",
    "list_project_root_versions",
    "restore_project_root_version",
    "summarize_versions",
]
