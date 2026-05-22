"""Conflict checkpoint primitives for Project thread drafts."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


CONFLICT_CHECKPOINTS_KEY = "conflict_checkpoints"


def normalise_conflict_checkpoints(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, Mapping):
        return {}
    checkpoints: dict[str, dict[str, Any]] = {}
    for path, checkpoint in value.items():
        if isinstance(path, str) and isinstance(checkpoint, Mapping):
            checkpoints[path] = dict(checkpoint)
    return dict(sorted(checkpoints.items()))


def _entry_from_checkpoint(checkpoint: Mapping[str, Any], key: str) -> dict[str, Any] | None:
    entry = checkpoint.get(key)
    return dict(entry) if isinstance(entry, Mapping) else None


def conflict_checkpoint_resolves(
    checkpoint: Mapping[str, Any] | None,
    *,
    root_entry: dict[str, Any] | None,
    draft_entry: dict[str, Any] | None,
    allow_unmodified_checkpoint: bool,
) -> bool:
    """Return whether a checkpoint turns an old conflict into a publishable draft change.

    Unmodified checkpoint retries are only publishable when the caller explicitly acknowledges
    that the unchanged draft is the intended conflict resolution.
    """

    if not checkpoint:
        return False
    checkpoint_root = _entry_from_checkpoint(checkpoint, "root_entry")
    if checkpoint_root != root_entry:
        return False
    if allow_unmodified_checkpoint:
        return True
    checkpoint_draft = _entry_from_checkpoint(checkpoint, "draft_entry")
    return checkpoint_draft != draft_entry


__all__ = [
    "CONFLICT_CHECKPOINTS_KEY",
    "conflict_checkpoint_resolves",
    "normalise_conflict_checkpoints",
]
