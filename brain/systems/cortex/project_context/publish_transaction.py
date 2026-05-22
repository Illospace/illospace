"""Transactional helpers for publishing local Project roots."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from brain.systems.cortex.project_context.drafts import sync_draft_from_root
from brain.systems.cortex.project_context.versioning import (
    build_project_root_version_metadata,
    capture_project_root_version,
    restore_project_root_version,
)


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _clean_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _local_publish_root(source_path: str) -> Path:
    source = Path(source_path).expanduser()
    return source.parent if source.exists() and source.is_file() else source


def _local_group_source_root(group: Mapping[str, Any]) -> Path | None:
    target = _as_mapping(group.get("publish_target"))
    source_path = _clean_text(target.get("path"))
    if target.get("kind") != "local_path" or not source_path:
        return None
    return _local_publish_root(source_path)


def _local_group_workspace_path(group: Mapping[str, Any]) -> Path | None:
    workspace_path = _clean_text(group.get("workspace_path"))
    return Path(workspace_path).expanduser() if workspace_path else None


def local_publish_roots(groups: list[Mapping[str, Any]]) -> list[Path]:
    roots: list[Path] = []
    seen: set[str] = set()
    for group in groups:
        root = _local_group_source_root(group)
        if root is None:
            continue
        key = str(root)
        if key in seen:
            continue
        seen.add(key)
        roots.append(root)
    return roots


def _transaction_version_metadata(
    *,
    run_id: str | None,
    idea_id: str | None,
    actor_id: str | None,
    org_id: str | None,
    phase: str,
    groups: list[Mapping[str, Any]],
) -> dict[str, Any]:
    operations: list[dict[str, Any]] = []
    for group in groups:
        for operation in group.get("operations") or []:
            if isinstance(operation, Mapping):
                operations.append(dict(operation))
    return build_project_root_version_metadata(
        run_id=run_id,
        idea_id=idea_id,
        actor_id=actor_id,
        org_id=org_id,
        resource_id="project-root-transaction",
        mount_path="/",
        phase=phase,
        operations=operations,
    )


def capture_local_transaction_versions(
    groups: list[Mapping[str, Any]],
    *,
    run_id: str | None,
    idea_id: str | None,
    actor_id: str | None,
    org_id: str | None,
) -> dict[str, Any]:
    operation_groups = [group for group in groups if group.get("operations")]
    if len(operation_groups) <= 1:
        return {}
    versions: dict[str, Any] = {}
    for root in local_publish_roots(groups):
        versions[str(root)] = capture_project_root_version(
            root,
            label="before-draft-publish-transaction",
            metadata=_transaction_version_metadata(
                run_id=run_id,
                idea_id=idea_id,
                actor_id=actor_id,
                org_id=org_id,
                phase="before_transaction",
                groups=groups,
            ),
        )
    return versions


def rollback_local_transaction(
    groups: list[Mapping[str, Any]],
    before_versions: Mapping[str, Any],
) -> list[str]:
    errors: list[str] = []
    restored_roots: set[str] = set()
    for root_text, version in before_versions.items():
        root = Path(root_text).expanduser()
        try:
            restore_project_root_version(root, version.version_id)
            restored_roots.add(root_text)
        except Exception as exc:
            errors.append(f"{root}: {exc}")
    for group in groups:
        root = _local_group_source_root(group)
        workspace = _local_group_workspace_path(group)
        if root is None or workspace is None or str(root) not in restored_roots:
            continue
        try:
            sync_draft_from_root(root, workspace)
        except Exception as exc:
            errors.append(f"{workspace}: {exc}")
    return errors


__all__ = [
    "capture_local_transaction_versions",
    "local_publish_roots",
    "rollback_local_transaction",
]
