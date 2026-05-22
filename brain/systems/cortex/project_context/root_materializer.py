"""Project-native root draft materialization helpers."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from brain.systems.cortex.project_context.drafts import build_file_manifest, load_draft_metadata, sync_draft_from_root
from brain.systems.cortex.project_context.project_root import (
    PROJECT_ROOT_MOUNT_PATH,
    PROJECT_ROOT_RESOURCE_ID,
    PROJECT_ROOT_RESOURCE_KIND,
    ProjectRootImportCandidate,
    project_draft_root_path,
    project_key_from_context,
    project_root_path,
)
from brain.systems.cortex.project_context.resource_imports import (
    ProjectSeedPredicate,
    project_native_import_candidates,
)
from brain.systems.cortex.project_context.root_aliases import (
    adopt_existing_project_root_alias,
    explicit_project_root_alias_paths,
)
from brain.systems.cortex.project_context.root_imports import import_candidates_into_project_root_versioned


WorkspaceEntryFactory = Callable[[dict[str, Any], Path | str], dict[str, str]]


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
    adopted_from_root = adopt_existing_project_root_alias(
        source_root,
        explicit_project_root_alias_paths(
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
