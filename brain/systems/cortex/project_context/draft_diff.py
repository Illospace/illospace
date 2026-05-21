"""Diff previews for local Project thread drafts."""
from __future__ import annotations

from pathlib import Path
from typing import Any
import difflib

from brain.systems.cortex.project_context.drafts import (
    FileManifest,
    _base_snapshot_path,
    _normalise_manifest,
    build_file_manifest,
    load_draft_metadata,
    plan_draft_publish,
)


DIFF_FILE_LIMIT = 20
DIFF_LINE_LIMIT = 240
DIFF_TEXT_SIZE_LIMIT = 256 * 1024


def _file_for_relative(root: Path, relative_path: str) -> Path:
    root = Path(root)
    if root.is_file():
        return root
    return root / relative_path


def _read_diff_lines(path: Path | None) -> tuple[list[str], bool, str | None]:
    if path is None or not path.exists() or not path.is_file() or path.is_symlink():
        return [], False, None
    try:
        stat = path.stat()
        if stat.st_size > DIFF_TEXT_SIZE_LIMIT:
            return [], True, "file_too_large_for_inline_diff"
        data = path.read_bytes()
        if b"\x00" in data:
            return [], True, "binary_file"
        return data.decode("utf-8", errors="replace").splitlines(keepends=True), False, None
    except Exception as exc:
        return [], True, str(exc)


def _diff_operation(left_entry: dict[str, Any] | None, right_entry: dict[str, Any] | None) -> str:
    if left_entry is None and right_entry is not None:
        return "create"
    if left_entry is not None and right_entry is None:
        return "delete"
    if left_entry == right_entry:
        return "unchanged"
    return "update"


def _inline_file_diff(
    *,
    path: str,
    left_path: Path | None,
    right_path: Path | None,
    left_label: str,
    right_label: str,
    left_entry: dict[str, Any] | None,
    right_entry: dict[str, Any] | None,
    conflicted: bool,
    max_lines: int,
) -> dict[str, Any]:
    left_lines, left_truncated, left_error = _read_diff_lines(left_path)
    right_lines, right_truncated, right_error = _read_diff_lines(right_path)
    patch_lines = list(difflib.unified_diff(
        left_lines,
        right_lines,
        fromfile=f"{left_label}/{path}",
        tofile=f"{right_label}/{path}",
        lineterm="",
    ))
    patch_truncated = len(patch_lines) > max_lines
    if patch_truncated:
        patch_lines = patch_lines[:max_lines]
    return {
        "path": path,
        "operation": _diff_operation(left_entry, right_entry),
        "conflicted": conflicted,
        "left_path": str(left_path) if left_path else None,
        "right_path": str(right_path) if right_path else None,
        "left_entry": left_entry,
        "right_entry": right_entry,
        "patch": "\n".join(patch_lines),
        "truncated": bool(left_truncated or right_truncated or patch_truncated),
        "errors": [error for error in (left_error, right_error) if error],
    }


def _selected_diff_paths(
    *,
    paths: list[str] | None,
    source_root: Path,
    draft_root: Path,
    max_files: int,
    allow_conflict_checkpoint_publish: bool,
) -> tuple[list[str], bool, set[str]]:
    plan = plan_draft_publish(
        source_root,
        draft_root,
        allow_conflict_checkpoint_publish=allow_conflict_checkpoint_publish,
    )
    selected = sorted({
        path
        for path in (paths or [*plan.created, *plan.modified, *plan.deleted, *plan.conflicted, *plan.out_of_date])
        if path
    })
    truncated = len(selected) > max_files
    return selected[:max_files], truncated, set(plan.conflicted)


def _root_to_draft_diff(
    *,
    relative_path: str,
    source_root: Path,
    draft_root: Path,
    root_manifest: FileManifest,
    draft_manifest: FileManifest,
    conflicted: bool,
    max_lines: int,
) -> dict[str, Any]:
    root_entry = root_manifest.get(relative_path)
    draft_entry = draft_manifest.get(relative_path)
    return _inline_file_diff(
        path=relative_path,
        left_path=_file_for_relative(source_root, relative_path) if root_entry is not None else None,
        right_path=_file_for_relative(draft_root, relative_path) if draft_entry is not None else None,
        left_label="root",
        right_label="draft",
        left_entry=root_entry,
        right_entry=draft_entry,
        conflicted=conflicted,
        max_lines=max_lines,
    )


def _base_to_draft_diff(
    *,
    relative_path: str,
    draft_root: Path,
    base_manifest: FileManifest,
    draft_manifest: FileManifest,
    conflicted: bool,
    max_lines: int,
) -> dict[str, Any]:
    draft_entry = draft_manifest.get(relative_path)
    base_entry = base_manifest.get(relative_path)
    base_path = _base_snapshot_path(draft_root, relative_path)
    base_available = base_path.exists() and base_path.is_file()
    payload = _inline_file_diff(
        path=relative_path,
        left_path=base_path if base_available else None,
        right_path=_file_for_relative(draft_root, relative_path) if draft_entry is not None else None,
        left_label="base",
        right_label="draft",
        left_entry=base_entry if base_available else None,
        right_entry=draft_entry,
        conflicted=conflicted,
        max_lines=max_lines,
    )
    payload["base_available"] = base_available
    return payload


def build_draft_diff(
    source_root: Path,
    draft_root: Path,
    *,
    paths: list[str] | None = None,
    max_files: int = DIFF_FILE_LIMIT,
    max_lines: int = DIFF_LINE_LIMIT,
    allow_conflict_checkpoint_publish: bool = False,
) -> dict[str, Any]:
    """Build capped root-to-draft and base-to-draft diff previews for local resources."""

    source_root = Path(source_root)
    draft_root = Path(draft_root)
    metadata = load_draft_metadata(draft_root)
    base_manifest = _normalise_manifest(metadata.get("base_manifest"))
    root_manifest = build_file_manifest(source_root)
    draft_manifest = build_file_manifest(draft_root)
    selected, truncated_files, conflicted_paths = _selected_diff_paths(
        paths=paths,
        source_root=source_root,
        draft_root=draft_root,
        max_files=max_files,
        allow_conflict_checkpoint_publish=allow_conflict_checkpoint_publish,
    )
    return {
        "truncated": truncated_files,
        "path_count": len(selected),
        "root_to_draft": [
            _root_to_draft_diff(
                relative_path=relative_path,
                source_root=source_root,
                draft_root=draft_root,
                root_manifest=root_manifest,
                draft_manifest=draft_manifest,
                conflicted=relative_path in conflicted_paths,
                max_lines=max_lines,
            )
            for relative_path in selected
        ],
        "base_to_draft": [
            _base_to_draft_diff(
                relative_path=relative_path,
                draft_root=draft_root,
                base_manifest=base_manifest,
                draft_manifest=draft_manifest,
                conflicted=relative_path in conflicted_paths,
                max_lines=max_lines,
            )
            for relative_path in selected
        ],
    }


__all__ = ["build_draft_diff"]
