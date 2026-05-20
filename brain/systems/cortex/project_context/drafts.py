"""Pure Project draft helpers.

Drafts are writable overlays over a canonical Project root. The helpers in this
module keep the root read-only and model synchronization as a file-level
three-way comparison between the base manifest, the latest root, and the draft.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import hashlib
import json
import shutil


DRAFT_METADATA_DIR = ".illo-project-draft"
PROJECT_HISTORY_DIR = ".illo-project-history"
DRAFT_METADATA_FILE = "metadata.json"
DRAFT_METADATA_SCHEMA_VERSION = 1
IGNORED_DRAFT_DIRS = {DRAFT_METADATA_DIR, PROJECT_HISTORY_DIR}

FileManifest = dict[str, dict[str, Any]]


@dataclass(frozen=True)
class ProjectDraftSyncResult:
    source_root: Path
    draft_root: Path
    base_manifest: FileManifest
    root_manifest: FileManifest
    draft_manifest: FileManifest
    updated_base_manifest: FileManifest
    copied: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    preserved: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    out_of_date: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.conflicts


@dataclass(frozen=True)
class ProjectDraftPublishPlan:
    created: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    conflicted: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.conflicted


def _metadata_path(draft_root: Path) -> Path:
    return Path(draft_root) / DRAFT_METADATA_DIR / DRAFT_METADATA_FILE


def _is_draft_metadata_relative(path: Path) -> bool:
    return bool(path.parts) and path.parts[0] in IGNORED_DRAFT_DIRS


def _manifest_entry(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "kind": "file",
        "sha256": digest.hexdigest(),
        "size": path.stat().st_size,
    }


def _normalise_manifest(manifest: Mapping[str, Any] | None) -> FileManifest:
    if not manifest:
        return {}
    normalised: FileManifest = {}
    for path, entry in manifest.items():
        if not isinstance(path, str) or not isinstance(entry, Mapping):
            continue
        normalised[path] = dict(entry)
    return dict(sorted(normalised.items()))


def build_file_manifest(root: Path) -> FileManifest:
    """Build a deterministic manifest for regular files under ``root``."""

    root = Path(root)
    if not root.exists():
        return {}

    if root.is_file() and not root.is_symlink():
        paths = [root]
    else:
        paths = list(root.rglob("*"))
    manifest: FileManifest = {}
    for path in paths:
        relative = path.relative_to(root.parent if root.is_file() else root)
        if _is_draft_metadata_relative(relative):
            continue
        if not path.is_file() or path.is_symlink():
            continue
        manifest[relative.as_posix()] = _manifest_entry(path)
    return dict(sorted(manifest.items()))


def load_draft_metadata(draft_root: Path) -> dict[str, Any]:
    path = _metadata_path(Path(draft_root))
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        metadata = json.load(handle)
    return metadata if isinstance(metadata, dict) else {}


def save_draft_metadata(
    draft_root: Path,
    metadata: Mapping[str, Any] | None = None,
    **updates: Any,
) -> dict[str, Any]:
    payload = dict(metadata or {})
    payload.update(updates)
    payload.setdefault("schema_version", DRAFT_METADATA_SCHEMA_VERSION)

    path = _metadata_path(Path(draft_root))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    return payload


def _base_manifest_from_metadata(
    draft_root: Path,
    base_manifest: Mapping[str, Any] | None,
) -> FileManifest:
    if base_manifest is not None:
        return _normalise_manifest(base_manifest)
    metadata = load_draft_metadata(draft_root)
    return _normalise_manifest(metadata.get("base_manifest"))


def _path_entry(manifest: FileManifest, path: str) -> dict[str, Any] | None:
    return manifest.get(path)


def _copy_root_file(source_root: Path, draft_root: Path, relative_path: str) -> None:
    source = source_root if source_root.is_file() else source_root / relative_path
    destination = draft_root / relative_path
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_dir() and not destination.is_symlink():
        shutil.rmtree(destination)
    elif destination.exists() or destination.is_symlink():
        destination.unlink()
    shutil.copy2(source, destination)


def _remove_draft_path(draft_root: Path, relative_path: str) -> None:
    path = draft_root / relative_path
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists() or path.is_symlink():
        path.unlink()


def _prune_empty_parents(draft_root: Path, relative_path: str) -> None:
    parent = (draft_root / relative_path).parent
    root = draft_root.resolve()
    while parent != draft_root and parent.exists():
        try:
            parent.resolve().relative_to(root)
            parent.rmdir()
        except OSError:
            break
        except ValueError:
            break
        parent = parent.parent


def _sorted_paths(*manifests: FileManifest) -> list[str]:
    paths: set[str] = set()
    for manifest in manifests:
        paths.update(manifest)
    return sorted(paths)


def _entries_match(left: dict[str, Any] | None, right: dict[str, Any] | None) -> bool:
    return left == right


def _is_conflict(
    *,
    base_entry: dict[str, Any] | None,
    root_entry: dict[str, Any] | None,
    draft_entry: dict[str, Any] | None,
) -> bool:
    draft_changed = not _entries_match(draft_entry, base_entry)
    root_changed = not _entries_match(root_entry, base_entry)
    if not draft_changed or not root_changed:
        return False
    return not _entries_match(draft_entry, root_entry)


def _updated_base_manifest(
    paths: list[str],
    *,
    base_manifest: FileManifest,
    root_manifest: FileManifest,
    conflicts: set[str],
) -> FileManifest:
    updated: FileManifest = {}
    for path in paths:
        if path in conflicts:
            entry = base_manifest.get(path)
        else:
            entry = root_manifest.get(path)
        if entry is not None:
            updated[path] = dict(entry)
    return dict(sorted(updated.items()))


def sync_draft_from_root(
    source_root: Path,
    draft_root: Path,
    *,
    base_manifest: Mapping[str, Any] | None = None,
) -> ProjectDraftSyncResult:
    """Copy latest root changes into unmodified draft files and report conflicts."""

    source_root = Path(source_root)
    draft_root = Path(draft_root)
    draft_root.mkdir(parents=True, exist_ok=True)

    metadata = load_draft_metadata(draft_root)
    base = (
        _normalise_manifest(base_manifest)
        if base_manifest is not None
        else _normalise_manifest(metadata.get("base_manifest"))
    )
    root_manifest = build_file_manifest(source_root)
    draft_before = build_file_manifest(draft_root)

    copied: list[str] = []
    removed: list[str] = []
    preserved: list[str] = []
    conflicts: list[str] = []
    paths = _sorted_paths(base, root_manifest, draft_before)

    for relative_path in paths:
        base_entry = _path_entry(base, relative_path)
        root_entry = _path_entry(root_manifest, relative_path)
        draft_entry = _path_entry(draft_before, relative_path)

        draft_changed = not _entries_match(draft_entry, base_entry)
        root_changed = not _entries_match(root_entry, base_entry)
        if not draft_changed:
            if root_entry is None:
                if draft_entry is not None:
                    _remove_draft_path(draft_root, relative_path)
                    _prune_empty_parents(draft_root, relative_path)
                    removed.append(relative_path)
            elif not _entries_match(draft_entry, root_entry):
                _copy_root_file(source_root, draft_root, relative_path)
                copied.append(relative_path)
            continue

        if _entries_match(draft_entry, root_entry):
            continue
        if root_changed:
            conflicts.append(relative_path)
        preserved.append(relative_path)

    draft_after = build_file_manifest(draft_root)
    conflict_set = set(conflicts)
    updated_base = _updated_base_manifest(
        _sorted_paths(base, root_manifest, draft_after),
        base_manifest=base,
        root_manifest=root_manifest,
        conflicts=conflict_set,
    )
    save_draft_metadata(
        draft_root,
        metadata,
        base_manifest=updated_base,
        source_root=str(source_root),
    )

    return ProjectDraftSyncResult(
        source_root=source_root,
        draft_root=draft_root,
        base_manifest=base,
        root_manifest=root_manifest,
        draft_manifest=draft_after,
        updated_base_manifest=updated_base,
        copied=copied,
        removed=removed,
        preserved=preserved,
        conflicts=conflicts,
        out_of_date=list(conflicts),
    )


def plan_draft_publish(
    source_root: Path,
    draft_root: Path,
    base_manifest: Mapping[str, Any] | None = None,
) -> ProjectDraftPublishPlan:
    """Return draft changes that can be published back to the source root."""

    source_root = Path(source_root)
    draft_root = Path(draft_root)
    explicit_base = base_manifest is not None
    base = _base_manifest_from_metadata(draft_root, base_manifest)
    root_manifest = build_file_manifest(source_root)
    if not explicit_base and not base:
        base = root_manifest
    draft_manifest = build_file_manifest(draft_root)

    created: list[str] = []
    modified: list[str] = []
    deleted: list[str] = []
    conflicted: list[str] = []

    for relative_path in _sorted_paths(base, root_manifest, draft_manifest):
        base_entry = _path_entry(base, relative_path)
        root_entry = _path_entry(root_manifest, relative_path)
        draft_entry = _path_entry(draft_manifest, relative_path)

        draft_changed = not _entries_match(draft_entry, base_entry)
        if not draft_changed:
            continue
        if _is_conflict(base_entry=base_entry, root_entry=root_entry, draft_entry=draft_entry):
            conflicted.append(relative_path)
            continue
        if _entries_match(draft_entry, root_entry):
            continue
        if base_entry is None and draft_entry is not None:
            created.append(relative_path)
        elif base_entry is not None and draft_entry is None:
            deleted.append(relative_path)
        elif draft_entry is not None:
            modified.append(relative_path)

    return ProjectDraftPublishPlan(
        created=created,
        modified=modified,
        deleted=deleted,
        conflicted=conflicted,
    )
