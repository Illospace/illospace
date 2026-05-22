"""Explicit Project root alias adoption."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
import shutil

from brain.systems.cortex.project_context.drafts import build_file_manifest
from brain.systems.cortex.project_context.project_root import project_root_path


def _clean_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _extend_alias_values(values: list[Any], value: Any) -> None:
    if isinstance(value, list):
        values.extend(value)
    else:
        values.append(value)


def explicit_project_root_alias_paths(
    project_context: Mapping[str, Any],
    resources: Sequence[Mapping[str, Any]],
    *,
    workspace_root: Path,
    canonical_key: str,
) -> list[Path]:
    values: list[Any] = [
        project_context.get("slug"),
        project_context.get("project_key"),
        project_context.get("previous_project_key"),
    ]
    for key in ("root_alias", "root_aliases", "previous_project_keys"):
        _extend_alias_values(values, project_context.get(key))
    for resource in resources:
        if not isinstance(resource, Mapping):
            continue
        values.extend([resource.get("project_key"), resource.get("slug")])
        for key in ("root_alias", "root_aliases", "previous_project_keys"):
            _extend_alias_values(values, resource.get(key))

    canonical_root = project_root_path(workspace_root, canonical_key)
    aliases: list[Path] = []
    for value in values:
        text = _clean_text(value)
        if not text or text == canonical_key:
            continue
        alias = project_root_path(workspace_root, text)
        if alias != canonical_root and alias not in aliases:
            aliases.append(alias)
    return aliases


def adopt_existing_project_root_alias(source_root: Path, alias_roots: Sequence[Path]) -> str | None:
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


__all__ = [
    "adopt_existing_project_root_alias",
    "explicit_project_root_alias_paths",
]
