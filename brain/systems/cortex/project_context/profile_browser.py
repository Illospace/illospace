"""Browse any accessible Project root through a thread-scoped draft overlay."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from brain.systems.cortex.project_context.browser import (
    project_file_blob,
    project_file_payload,
    update_project_draft_file,
    with_project_file_browser,
)
from brain.systems.cortex.project_context.drafts import plan_draft_publish, sync_draft_from_root
from brain.systems.cortex.project_context.identity import stamped_project_context
from brain.systems.cortex.project_context.project_root import (
    PROJECT_ROOT_MOUNT_PATH,
    PROJECT_ROOT_RESOURCE_ID,
    PROJECT_ROOT_RESOURCE_KIND,
    project_draft_root_path,
    project_key_from_context,
    project_root_path,
)
from brain.systems.cortex.project_context.profiles import profile_to_read
from brain.systems.cortex.project_context.workspace_manifest import PROJECT_CONTEXT_DIR


class ProjectProfileBrowserError(ValueError):
    """Raised when a Project profile cannot be browsed in the current thread."""


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _project_resources(profile: Any) -> list[dict[str, Any]]:
    context = profile.project_context if isinstance(profile.project_context, Mapping) else {}
    return [dict(item) for item in (context.get("resources") or []) if isinstance(item, Mapping)]


def _run_project_manifests(run: Any) -> list[dict[str, Any]]:
    manifests: list[dict[str, Any]] = []
    for payload in (
        _mapping(getattr(run, "workspace_ref", None)),
        _mapping(getattr(run, "metadata_", None)),
        _mapping(getattr(run, "target_ref", None)),
    ):
        direct = _mapping(payload.get("project_workspace_manifest"))
        if direct:
            manifests.append(direct)
        materialization = _mapping(payload.get("project_context_materialization"))
        nested = _mapping(materialization.get("workspace_manifest"))
        if nested:
            manifests.append(nested)
    return manifests


def _thread_workspace_root_from_path(path: Any) -> Path | None:
    text = str(path or "").strip()
    if not text:
        return None
    parts = Path(text).expanduser().parts
    if PROJECT_CONTEXT_DIR not in parts:
        return None
    index = parts.index(PROJECT_CONTEXT_DIR)
    return Path(*parts[:index]) if index > 0 else None


def _thread_workspace_root_for_run(run: Any) -> Path | None:
    for manifest in _run_project_manifests(run):
        for mount in manifest.get("mounts") or []:
            mount_payload = _mapping(mount)
            draft_identity = _mapping(mount_payload.get("draft_identity"))
            thread_root = draft_identity.get("thread_workspace_root")
            if isinstance(thread_root, str) and thread_root.strip():
                return Path(thread_root).expanduser()
            derived = _thread_workspace_root_from_path(
                mount_payload.get("workspace_path")
                or mount_payload.get("resource_path")
                or mount_payload.get("source_path")
            )
            if derived is not None:
                return derived
        derived = _thread_workspace_root_from_path(manifest.get("workspace_root"))
        if derived is not None:
            return derived

    for payload in (
        _mapping(getattr(run, "workspace_ref", None)),
        _mapping(getattr(run, "metadata_", None)),
    ):
        for key in ("resolved_workspace_root", "workspace_root", "workspace_hint"):
            derived = _thread_workspace_root_from_path(payload.get(key))
            if derived is not None:
                return derived
        for workspace in payload.get("workspaces") or []:
            derived = _thread_workspace_root_from_path(_mapping(workspace).get("path"))
            if derived is not None:
                return derived
    return None


def _profile_project_key(profile: Any) -> str:
    context = stamped_project_context(profile)
    return project_key_from_context(
        context,
        resources=_project_resources(profile),
        fallback=getattr(profile, "slug", None) or getattr(profile, "id", None),
    )


def _draft_change_payload(root_path: Path, draft_path: Path) -> dict[str, Any]:
    paths = {
        "changed_paths": [],
        "new_paths": [],
        "deleted_paths": [],
        "conflicted_paths": [],
        "out_of_date_paths": [],
    }
    if draft_path.exists():
        plan = plan_draft_publish(root_path, draft_path)
        paths.update({
            "changed_paths": plan.modified,
            "new_paths": plan.created,
            "deleted_paths": plan.deleted,
            "conflicted_paths": plan.conflicted,
            "out_of_date_paths": plan.out_of_date,
        })
    return {
        **paths,
        "counts": {
            key: len(paths[key])
            for key in ("changed_paths", "new_paths", "deleted_paths", "conflicted_paths")
        },
        "total": sum(len(paths[key]) for key in ("changed_paths", "new_paths", "deleted_paths", "conflicted_paths")),
    }


def _project_root_resource(profile: Any, run: Any, *, create_draft: bool = False) -> dict[str, Any]:
    thread_root = _thread_workspace_root_for_run(run)
    if thread_root is None:
        raise ProjectProfileBrowserError("No thread workspace root is available for Project browsing.")

    project_key = _profile_project_key(profile)
    root_path = project_root_path(thread_root, project_key)
    root_path.mkdir(parents=True, exist_ok=True)
    draft_path = project_draft_root_path(thread_root, project_key)
    if create_draft and not draft_path.exists():
        sync_draft_from_root(root_path, draft_path)

    changes = _draft_change_payload(root_path, draft_path)
    resource: dict[str, Any] = {
        "id": PROJECT_ROOT_RESOURCE_ID,
        "label": profile.name or profile.slug or "Project root",
        "kind": PROJECT_ROOT_RESOURCE_KIND,
        "mount_path": PROJECT_ROOT_MOUNT_PATH,
        "source_path": str(root_path),
        "project_profile_id": str(profile.id),
        "project_key": project_key,
        "changes": changes,
        "change_counts": changes["counts"],
        "out_of_date_paths": changes["out_of_date_paths"],
        "out_of_date": bool(changes["out_of_date_paths"]),
        "status": (
            "conflicted"
            if changes["conflicted_paths"]
            else "out_of_date"
            if changes["out_of_date_paths"] and changes["total"] == 0
            else "modified"
            if changes["total"] > 0
            else "clean"
        ),
    }
    if draft_path.exists():
        resource.update({
            "workspace_path": str(draft_path),
            "resource_path": str(draft_path),
            "is_draft_workspace": True,
            "materialization": {
                "provider": "project_root",
                "draft": True,
                "project_key": project_key,
                "path": str(draft_path),
                "workspace_path": str(draft_path),
                "source_path": str(root_path),
            },
        })
    return resource


def _aggregate_changes(resource: Mapping[str, Any]) -> dict[str, Any]:
    changes = _mapping(resource.get("changes"))
    aggregate = {
        key: [
            {"resource_id": resource["id"], "mount_path": resource["mount_path"], "path": path}
            for path in changes.get(key) or []
        ]
        for key in ("changed_paths", "new_paths", "deleted_paths", "conflicted_paths", "out_of_date_paths")
    }
    aggregate["counts"] = {
        key: len(aggregate[key])
        for key in ("changed_paths", "new_paths", "deleted_paths", "conflicted_paths")
    }
    aggregate["total"] = sum(aggregate["counts"].values())
    return aggregate


def project_profile_draft_status_payload(profile: Any, run: Any, *, idea_id: str) -> dict[str, Any]:
    resource = _project_root_resource(profile, run)
    return with_project_file_browser({
        "ok": True,
        "action": "profile_draft_status",
        "idea_id": str(idea_id),
        "run_id": str(getattr(run, "id", "") or ""),
        "project_profile_id": str(profile.id),
        "project": profile_to_read(profile).model_dump(mode="json", by_alias=True),
        "resources": [resource],
        "changes": _aggregate_changes(resource),
    })


def project_profile_publish_plan_payload(draft_status: Mapping[str, Any]) -> dict[str, Any]:
    resources = [item for item in draft_status.get("resources") or [] if isinstance(item, Mapping)]
    groups: list[dict[str, Any]] = []
    operation_count = 0
    blocked_count = 0
    for resource in resources:
        changes = _mapping(resource.get("changes"))
        operations = [
            {"operation": "update", "path": path}
            for path in changes.get("changed_paths") or []
        ] + [
            {"operation": "create", "path": path}
            for path in changes.get("new_paths") or []
        ] + [
            {"operation": "delete", "path": path}
            for path in changes.get("deleted_paths") or []
        ]
        operation_count += len(operations)
        blocked = len(changes.get("conflicted_paths") or [])
        blocked_count += 1 if blocked else 0
        groups.append({
            "resource_id": resource.get("id"),
            "mount_path": resource.get("mount_path"),
            "label": resource.get("label"),
            "workspace_path": resource.get("workspace_path"),
            "publish_target": {"kind": "local_path", "path": resource.get("source_path")},
            "status": "blocked" if blocked else "ready" if operations else "clean",
            "blocked_reasons": ["conflicts"] if blocked else [],
            "change_counts": resource.get("change_counts") or {},
            "operations": operations,
        })
    return {
        "ok": True,
        "action": "plan_publish",
        "mutates_project_root": True,
        "plan_only": True,
        "summary": {
            "resource_count": len(groups),
            "operation_count": operation_count,
            "blocked_count": blocked_count,
        },
        "groups": groups,
    }


def project_profile_draft_state_payload(profile: Any, run: Any, *, idea_id: str) -> dict[str, Any]:
    draft_status = project_profile_draft_status_payload(profile, run, idea_id=idea_id)
    return {
        "ok": True,
        "idea_id": str(idea_id),
        "run_id": str(getattr(run, "id", "") or ""),
        "project_profile_id": str(profile.id),
        "project": profile_to_read(profile).model_dump(mode="json", by_alias=True),
        "draft_status": draft_status,
        "plan_publish": project_profile_publish_plan_payload(draft_status),
        "root_versions": {
            "ok": True,
            "action": "root_versions",
            "summary": {"resource_count": 1, "version_count": 0},
            "groups": [],
        },
    }


def project_profile_file_payload(profile: Any, run: Any, *, idea_id: str, path: str) -> dict[str, Any]:
    draft_status = project_profile_draft_status_payload(profile, run, idea_id=idea_id)
    return project_file_payload(draft_status, resource_id=PROJECT_ROOT_RESOURCE_ID, path=path)


def project_profile_file_blob(profile: Any, run: Any, *, idea_id: str, path: str, layer: str) -> dict[str, Any]:
    draft_status = project_profile_draft_status_payload(profile, run, idea_id=idea_id)
    return project_file_blob(draft_status, resource_id=PROJECT_ROOT_RESOURCE_ID, path=path, layer=layer)


def update_project_profile_draft_file(
    profile: Any,
    run: Any,
    *,
    idea_id: str,
    path: str,
    content: str,
) -> dict[str, Any]:
    resource = _project_root_resource(profile, run, create_draft=True)
    draft_status = with_project_file_browser({
        "ok": True,
        "action": "profile_draft_status",
        "idea_id": str(idea_id),
        "run_id": str(getattr(run, "id", "") or ""),
        "project_profile_id": str(profile.id),
        "resources": [resource],
        "changes": _aggregate_changes(resource),
    })
    return update_project_draft_file(
        draft_status,
        resource_id=PROJECT_ROOT_RESOURCE_ID,
        path=path,
        content=content,
    )
