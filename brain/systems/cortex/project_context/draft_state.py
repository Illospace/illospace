"""Resolve Project draft context and status payloads."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from brain.systems.cortex.project_context.drafts import (
    plan_draft_publish,
    sync_draft_from_root,
)
from brain.systems.cortex.project_context.repo_publish import repo_draft_status, repo_draft_upstream_status
from brain.systems.cortex.project_context.runtime_context import (
    project_runtime_context_from_payloads,
)
from brain.systems.cortex.project_context.workspace_manifest import ProjectWorkspaceManifest
from brain.systems.runs.execution_context import current_agent_context


PROJECT_DRAFT_CHANGE_LIMIT = 500


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _first_mapping(*values: Any) -> dict[str, Any]:
    for value in values:
        mapped = _as_mapping(value)
        if mapped:
            return mapped
    return {}


def _clean_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _run_identifier(run: Any, metadata: Mapping[str, Any], execution_metadata: Mapping[str, Any]) -> str | None:
    context = current_agent_context()
    for value in (
        getattr(run, "id", None),
        getattr(run, "run_id", None),
        getattr(context, "run_id", None),
        metadata.get("run_id"),
        execution_metadata.get("run_id"),
    ):
        if value is not None and str(value).strip():
            return str(value)
    return None


def _current_project_draft_context() -> dict[str, Any]:
    context = current_agent_context()
    run = getattr(context, "run", None)
    execution_metadata = _as_mapping(getattr(context, "execution_metadata", None))
    metadata = _first_mapping(getattr(run, "metadata_", None), getattr(run, "metadata", None), execution_metadata)
    target_ref = _first_mapping(
        getattr(run, "target_ref", None),
        getattr(run, "target_metadata", None),
        getattr(context, "target_ref", None),
        metadata.get("target_ref"),
        execution_metadata.get("target_ref"),
    )
    workspace_ref = _first_mapping(
        getattr(run, "workspace_ref", None),
        getattr(context, "workspace_ref", None),
        metadata.get("workspace_ref"),
        execution_metadata.get("workspace_ref"),
    )
    runtime = project_runtime_context_from_payloads(workspace_ref, target_ref, metadata, execution_metadata)
    snapshot = _first_mapping(
        runtime.get("project_context_snapshot"),
    )
    manifest = _first_mapping(
        runtime.get("project_workspace_manifest"),
    )
    materialization = _first_mapping(
        runtime.get("project_context_materialization"),
    )
    idea_id = (
        _clean_text(getattr(context, "idea_id", None))
        or _clean_text(target_ref.get("idea_id"))
        or _clean_text(target_ref.get("thread_id"))
        or _clean_text(metadata.get("idea_id"))
        or _clean_text(execution_metadata.get("idea_id"))
    )

    return {
        "run": run,
        "run_id": _run_identifier(run, metadata, execution_metadata),
        "idea_id": idea_id,
        "metadata": metadata,
        "target_ref": target_ref,
        "workspace_ref": workspace_ref,
        "snapshot": snapshot,
        "manifest": manifest,
        "materialization": materialization,
    }


def _empty_change_set() -> dict[str, list[str]]:
    return {
        "changed_paths": [],
        "new_paths": [],
        "deleted_paths": [],
        "conflicted_paths": [],
    }


def _limited_unique_paths(paths: list[str]) -> list[str]:
    deduped = sorted({path for path in paths if path})
    return deduped[:PROJECT_DRAFT_CHANGE_LIMIT]


def _dedupe_change_set(changes: dict[str, list[str]]) -> dict[str, list[str]]:
    normalized = _empty_change_set()
    normalized.update(changes)
    return {key: _limited_unique_paths(values) for key, values in normalized.items()}


def _draft_publish_change_set(
    draft_path: str | None,
    source_path: str | None,
    *,
    allow_conflict_checkpoint_publish: bool = False,
) -> tuple[dict[str, list[str]], str, list[str], list[str], dict[str, Any]]:
    if not draft_path or not source_path:
        return _empty_change_set(), "draft_manifest", [], [], {}
    draft = Path(draft_path).expanduser()
    source = Path(source_path).expanduser()
    if not draft.exists() or not source.exists():
        return _empty_change_set(), "draft_manifest", [], [], {}
    plan = plan_draft_publish(
        source,
        draft,
        allow_conflict_checkpoint_publish=allow_conflict_checkpoint_publish,
    )
    return _dedupe_change_set({
        "changed_paths": plan.modified,
        "new_paths": plan.created,
        "deleted_paths": plan.deleted,
        "conflicted_paths": plan.conflicted,
    }), "draft_manifest", _limited_unique_paths(plan.out_of_date), [], {}


def _repo_change_set(
    path: str | None,
    *,
    mount_subpath: str | None = None,
    repo_status=None,
    repo_upstream_status=None,
    base_branch: str | None = None,
) -> tuple[dict[str, list[str]], str, list[str], list[str], dict[str, Any]]:
    if not path:
        return _empty_change_set(), "repo_status", [], [], {}
    repo_status = repo_status or repo_draft_status
    repo_upstream_status = repo_upstream_status or repo_draft_upstream_status
    repo_path = Path(path).expanduser()
    scope_kwargs = {"mount_subpath": mount_subpath} if mount_subpath else {}
    status = repo_status(repo_path, **scope_kwargs)
    errors = [str(error) for error in (getattr(status, "errors", None) or []) if str(error)]
    changed_paths = [path for path in status.changed_paths if path not in set(status.unmerged_paths)]
    upstream_changed_paths: list[str] = []
    upstream_conflicted_paths: list[str] = []
    upstream_errors: list[str] = []
    upstream_status = "not_checked"
    if not errors:
        try:
            upstream = repo_upstream_status(
                repo_path,
                changed_paths=status.changed_paths,
                base_branch=base_branch,
                fetch=True,
                **scope_kwargs,
            )
            upstream_status = str(getattr(upstream, "status", None) or "not_checked")
            upstream_changed_paths = [
                str(path)
                for path in (getattr(upstream, "upstream_changed_paths", None) or [])
                if str(path)
            ]
            upstream_conflicted_paths = [
                str(path)
                for path in (getattr(upstream, "upstream_conflicted_paths", None) or [])
                if str(path)
            ]
            upstream_errors = [str(error) for error in (getattr(upstream, "errors", None) or []) if str(error)]
            if upstream_errors:
                upstream_status = "error"
        except Exception as exc:
            upstream_status = "error"
            upstream_errors = [str(exc)]
    return _dedupe_change_set({
        "changed_paths": changed_paths,
        "new_paths": [],
        "deleted_paths": [],
        "conflicted_paths": [*status.unmerged_paths, *upstream_conflicted_paths],
    }), "repo_status", _limited_unique_paths(upstream_changed_paths), errors, {
        "upstream_status": upstream_status,
        "upstream_changed_paths": _limited_unique_paths(upstream_changed_paths),
        "upstream_conflicted_paths": _limited_unique_paths(upstream_conflicted_paths),
        "upstream_errors": upstream_errors,
    }


def _manifest_resources(snapshot: Mapping[str, Any], manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    mounts = [mount for mount in manifest.get("mounts") or [] if isinstance(mount, Mapping)]
    if not mounts:
        return [
            dict(resource)
            for resource in snapshot.get("resources") or []
            if isinstance(resource, Mapping)
        ]

    raw_resources = [
        dict(resource)
        for resource in snapshot.get("resources") or []
        if isinstance(resource, Mapping)
    ]
    by_id = {str(resource.get("id") or ""): resource for resource in raw_resources}
    resources: list[dict[str, Any]] = []
    for index, mount in enumerate(mounts):
        resource_id = str(mount.get("resource_id") or mount.get("id") or f"resource-{index + 1}")
        resource = dict(by_id.get(resource_id, {}))
        materialization = _as_mapping(resource.get("materialization"))
        mount_metadata = _as_mapping(mount.get("metadata"))
        mount_materialization = _as_mapping(mount_metadata.get("materialization"))
        workspace_path = _clean_text(mount.get("workspace_path"))
        resource_path = _clean_text(mount.get("resource_path")) or workspace_path
        source_path = _clean_text(mount.get("source_path")) or _clean_text(mount_materialization.get("source_path"))
        materialization.update(mount_materialization)
        if resource_path:
            materialization["path"] = resource_path
        if workspace_path:
            materialization["workspace_path"] = workspace_path
        if source_path:
            materialization["source_path"] = source_path
            materialization["draft"] = True
        if mount.get("repo"):
            materialization["repo"] = mount.get("repo")
        resource.update({
            "id": resource_id,
            "kind": _clean_text(mount.get("kind")) or _clean_text(resource.get("kind")) or "resource",
            "mount_path": _clean_text(mount.get("mount_path")) or _clean_text(resource.get("mount_path")),
            "path": resource_path or _clean_text(resource.get("path")),
            "workspace_path": workspace_path or _clean_text(resource.get("workspace_path")),
            "source_path": source_path or _clean_text(resource.get("source_path")),
            "repo": _clean_text(mount.get("repo")) or _clean_text(resource.get("repo")),
            "materialization": materialization,
        })
        resources.append(resource)
    return resources


def _resource_label(resource: Mapping[str, Any], workspace_path: str | None) -> str:
    return (
        _clean_text(resource.get("mount_path"))
        or _clean_text(resource.get("project_path"))
        or _clean_text(resource.get("repo"))
        or _clean_text(resource.get("name"))
        or _clean_text(resource.get("label"))
        or (Path(workspace_path).name if workspace_path else None)
        or "Project resource"
    )


def _path_in_project_context_workspace(path: str | None) -> bool:
    if not path:
        return False
    try:
        return ".illo-project-context" in Path(path).expanduser().parts
    except Exception:
        return ".illo-project-context" in str(path)


def _is_repo_resource(resource: Mapping[str, Any], materialization: Mapping[str, Any]) -> bool:
    kind = _clean_text(resource.get("kind") or resource.get("type") or resource.get("resource_type"))
    provider = _clean_text(materialization.get("provider") or resource.get("provider"))
    return bool(
        _clean_text(materialization.get("repo") or resource.get("repo"))
        or kind == "repo"
        or provider in {"github", "git"}
    )


def _resource_draft_entry(
    resource: Mapping[str, Any],
    index: int,
    *,
    repo_status=None,
    repo_upstream_status=None,
    allow_conflict_checkpoint_publish: bool = False,
) -> dict[str, Any]:
    materialization = _as_mapping(resource.get("materialization"))
    resource_path = _clean_text(materialization.get("path")) or _clean_text(resource.get("path"))
    workspace_path = _clean_text(materialization.get("workspace_path")) or _clean_text(resource.get("workspace_path"))
    if not workspace_path and resource_path:
        path = Path(resource_path).expanduser()
        workspace_path = str(path.parent if path.exists() and path.is_file() else path)
    source_path = _clean_text(materialization.get("source_path")) or _clean_text(resource.get("source_path"))
    repo_path = _clean_text(materialization.get("repo_path"))
    repo_subpath = _clean_text(materialization.get("subpath"))

    if source_path:
        changes, change_source, out_of_date_paths, status_errors, status_details = _draft_publish_change_set(
            workspace_path or resource_path,
            source_path,
            allow_conflict_checkpoint_publish=allow_conflict_checkpoint_publish,
        )
    elif _is_repo_resource(resource, materialization):
        changes, change_source, out_of_date_paths, status_errors, status_details = _repo_change_set(
            repo_path or workspace_path or resource_path,
            mount_subpath=repo_subpath,
            repo_status=repo_status,
            repo_upstream_status=repo_upstream_status,
            base_branch=_clean_text(resource.get("default_branch") or materialization.get("branch")),
        )
    else:
        changes, change_source, out_of_date_paths, status_errors, status_details = (
            _empty_change_set(),
            "untracked_resource",
            [],
            [],
            {},
        )

    if out_of_date_paths:
        changes["out_of_date_paths"] = out_of_date_paths

    change_counts = {key: len(value) for key, value in changes.items()}
    actionable_change_total = sum(
        change_counts.get(key, 0)
        for key in ("changed_paths", "new_paths", "deleted_paths", "conflicted_paths")
    )
    status = (
        "error"
        if status_errors
        else "conflicted"
        if changes["conflicted_paths"]
        else "out_of_date"
        if out_of_date_paths and not actionable_change_total
        else "modified"
        if actionable_change_total
        else "clean"
    )
    label = _resource_label(resource, workspace_path or resource_path)
    return {
        "id": str(resource.get("id") or f"resource-{index + 1}"),
        "label": label,
        "mount_path": _clean_text(resource.get("mount_path")) or _clean_text(resource.get("project_path")) or label,
        "kind": _clean_text(resource.get("kind") or resource.get("type") or resource.get("resource_type")) or "resource",
        "provider": _clean_text(materialization.get("provider")) or _clean_text(resource.get("provider")),
        "repo": _clean_text(materialization.get("repo")) or _clean_text(resource.get("repo")),
        "repo_path": repo_path,
        "repo_subpath": repo_subpath,
        "workspace_path": workspace_path,
        "resource_path": resource_path,
        "source_path": source_path,
        "is_draft_workspace": bool(materialization.get("draft"))
        or _path_in_project_context_workspace(workspace_path or resource_path),
        "status": status,
        "metadata_available": change_source == "draft_manifest",
        "change_source": change_source,
        "change_counts": change_counts,
        "out_of_date": bool(out_of_date_paths),
        "out_of_date_paths": out_of_date_paths,
        "errors": status_errors,
        "details": status_details,
        "changes": changes,
    }


def _project_draft_resources(
    snapshot: Mapping[str, Any],
    *,
    manifest: Mapping[str, Any] | None = None,
    repo_status=None,
    repo_upstream_status=None,
    allow_conflict_checkpoint_publish: bool = False,
) -> list[dict[str, Any]]:
    resources = _manifest_resources(snapshot, manifest or {})
    return [
        _resource_draft_entry(
            resource,
            index,
            repo_status=repo_status,
            repo_upstream_status=repo_upstream_status,
            allow_conflict_checkpoint_publish=allow_conflict_checkpoint_publish,
        )
        for index, resource in enumerate(resources)
        if isinstance(resource, Mapping)
    ]


def _aggregate_resource_changes(resources: list[dict[str, Any]]) -> dict[str, Any]:
    paths = {key: [] for key in _empty_change_set()}
    out_of_date_paths: list[dict[str, Any]] = []
    for resource in resources:
        changes = resource.get("changes") if isinstance(resource.get("changes"), dict) else {}
        for key in paths:
            for path in changes.get(key) or []:
                paths[key].append({
                    "resource_id": resource.get("id"),
                    "mount_path": resource.get("mount_path"),
                    "path": path,
                })
        for path in changes.get("out_of_date_paths") or resource.get("out_of_date_paths") or []:
            out_of_date_paths.append({
                "resource_id": resource.get("id"),
                "mount_path": resource.get("mount_path"),
                "path": path,
            })
    return {
        **paths,
        "out_of_date_paths": out_of_date_paths,
        "counts": {key: len(value) for key, value in paths.items()},
        "total": sum(len(value) for value in paths.values()),
    }


def project_draft_status_payload(
    *,
    repo_status=None,
    repo_upstream_status=None,
    allow_conflict_checkpoint_publish: bool = False,
) -> dict[str, Any]:
    context = _current_project_draft_context()
    if not context["run_id"] and not context["idea_id"] and not context["snapshot"]:
        return {
            "ok": False,
            "code": "project_thread_not_bound",
            "error": "draft_status requires a current AgentRun or Cortex thread with Project Context attached.",
        }

    snapshot = context["snapshot"]
    if not snapshot:
        return {
            "ok": False,
            "code": "project_draft_not_bound",
            "error": "No Project draft workspace is bound to the current run/thread.",
            "run_id": context["run_id"],
            "idea_id": context["idea_id"],
        }

    manifest = _as_mapping(context["manifest"])
    if not manifest:
        manifest = ProjectWorkspaceManifest.from_project_context(snapshot).to_dict()
    resources = _project_draft_resources(
        snapshot,
        manifest=manifest,
        repo_status=repo_status,
        repo_upstream_status=repo_upstream_status,
        allow_conflict_checkpoint_publish=allow_conflict_checkpoint_publish,
    )
    snapshot_resources = snapshot.get("resources") if isinstance(snapshot.get("resources"), list) else []
    manifest_mounts = manifest.get("mounts") if isinstance(manifest.get("mounts"), list) else []
    if not resources and (snapshot_resources or manifest_mounts):
        return {
            "ok": False,
            "code": "project_draft_not_materialized",
            "error": "Project Context is attached, but no materialized draft workspace resources were found.",
            "run_id": context["run_id"],
            "idea_id": context["idea_id"],
        }

    workspaces = list((_as_mapping(context["manifest"]).get("workspaces") or []))
    return {
        "ok": True,
        "action": "draft_status",
        "run_id": context["run_id"],
        "idea_id": context["idea_id"],
        "workspaces": workspaces,
        "workspace_manifest": context["manifest"],
        "materialization": context["materialization"],
        "resources": resources,
        "changes": _aggregate_resource_changes(resources),
    }


def _resource_matches_filter(resource: Mapping[str, Any], resource_id: str) -> bool:
    materialization = _as_mapping(resource.get("materialization"))
    candidates = {
        str(resource.get("id") or ""),
        str(resource.get("mount_path") or ""),
        str(resource.get("path") or ""),
        str(resource.get("workspace_path") or ""),
        str(resource.get("source_path") or ""),
        str(materialization.get("path") or ""),
        str(materialization.get("workspace_path") or ""),
        str(materialization.get("source_path") or ""),
    }
    return resource_id in candidates


def project_refresh_draft_from_root_payload(
    *,
    resource_id: str | None = None,
    resource_ids: list[str] | None = None,
) -> dict[str, Any]:
    context = _current_project_draft_context()
    if not context["run_id"] and not context["idea_id"] and not context["snapshot"]:
        return {
            "ok": False,
            "code": "project_thread_not_bound",
            "error": "refresh_draft_from_root requires a current AgentRun or Cortex thread with Project Context attached.",
        }

    snapshot = context["snapshot"]
    if not snapshot:
        return {
            "ok": False,
            "code": "project_draft_not_bound",
            "error": "No Project draft workspace is bound to the current run/thread.",
            "run_id": context["run_id"],
            "idea_id": context["idea_id"],
        }

    manifest = _as_mapping(context["manifest"]) or ProjectWorkspaceManifest.from_project_context(snapshot).to_dict()
    resources = _manifest_resources(snapshot, manifest)
    selected_ids = {str(value) for value in (resource_ids or []) if str(value).strip()}
    if resource_id:
        selected_ids.add(str(resource_id))

    refreshed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for index, resource in enumerate(resources):
        if selected_ids and not any(_resource_matches_filter(resource, selected) for selected in selected_ids):
            continue
        materialization = _as_mapping(resource.get("materialization"))
        resource_path = _clean_text(materialization.get("path")) or _clean_text(resource.get("path"))
        workspace_path = _clean_text(materialization.get("workspace_path")) or _clean_text(resource.get("workspace_path"))
        source_path = _clean_text(materialization.get("source_path")) or _clean_text(resource.get("source_path"))
        label = _resource_label(resource, workspace_path or resource_path)
        base_payload = {
            "resource_id": str(resource.get("id") or f"resource-{index + 1}"),
            "mount_path": _clean_text(resource.get("mount_path")) or label,
            "label": label,
            "workspace_path": workspace_path or resource_path,
            "source_path": source_path,
        }
        if not source_path:
            skipped.append({**base_payload, "status": "skipped", "reason": "resource_has_no_local_project_root"})
            continue
        if not (workspace_path or resource_path):
            skipped.append({**base_payload, "status": "skipped", "reason": "resource_has_no_draft_workspace"})
            continue
        source = Path(source_path).expanduser()
        draft = Path(workspace_path or resource_path).expanduser()
        if not source.exists():
            skipped.append({**base_payload, "status": "skipped", "reason": "project_root_missing"})
            continue
        try:
            result = sync_draft_from_root(source, draft)
        except Exception as exc:
            skipped.append({**base_payload, "status": "error", "reason": "refresh_failed", "error": str(exc)})
            continue
        refreshed.append({
            **base_payload,
            "status": "refreshed" if result.ok else "conflicted",
            "updated_from_root": result.copied,
            "removed_from_root": result.removed,
            "preserved_draft_paths": result.preserved,
            "conflicted_paths": result.conflicts,
            "out_of_date_paths": result.out_of_date,
        })

    if selected_ids and not refreshed and not skipped:
        return {
            "ok": False,
            "action": "refresh_draft_from_root",
            "code": "project_refresh_selection_empty",
            "error": "No Project draft resources matched the requested refresh filters.",
            "selected_resource_ids": sorted(selected_ids),
        }

    return {
        "ok": True,
        "action": "refresh_draft_from_root",
        "run_id": context["run_id"],
        "idea_id": context["idea_id"],
        "mutated_project_root": False,
        "mutated_draft_workspace": bool(refreshed),
        "summary": {
            "refreshed_count": len(refreshed),
            "skipped_count": len(skipped),
            "conflicted_count": sum(1 for item in refreshed if item.get("conflicted_paths")),
        },
        "refreshed_resources": refreshed,
        "skipped_resources": skipped,
    }


__all__ = [
    "PROJECT_DRAFT_CHANGE_LIMIT",
    "project_refresh_draft_from_root_payload",
    "project_draft_status_payload",
]
