"""Plan and publish Project draft changes."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any
import shutil

from brain.systems.cortex.project_context.draft_state import project_draft_status_payload
from brain.systems.cortex.project_context.draft_diff import build_draft_diff
from brain.systems.cortex.project_context.drafts import (
    build_file_manifest,
    clear_conflict_checkpoints,
    load_draft_metadata,
    record_conflict_checkpoints,
    refresh_base_snapshots_for_paths,
    save_draft_metadata,
    sync_draft_from_root,
)
from brain.systems.cortex.project_context.repo_publish import (
    publish_repo_draft,
    repo_draft_status,
)
from brain.systems.cortex.project_context.versioning import (
    build_project_root_version_metadata,
    capture_project_root_version,
    restore_project_root_version,
)
from brain.systems.runs.execution_context import current_agent_context


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _clean_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _paths_from_value(value: Any) -> list[str]:
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    if not isinstance(value, list):
        return []
    paths: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            paths.append(item.strip())
        elif isinstance(item, Mapping):
            path = _clean_text(item.get("path") or item.get("relative_path") or item.get("name"))
            if path:
                paths.append(path)
    return paths


def _strings_from_value(value: Any) -> list[str]:
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _current_publish_actor() -> tuple[str | None, str | None]:
    context = current_agent_context()
    run = getattr(context, "run", None)
    metadata = _as_mapping(getattr(run, "metadata_", None)) or _as_mapping(getattr(run, "metadata", None))
    execution_metadata = _as_mapping(getattr(context, "execution_metadata", None))
    actor_id = (
        _clean_text(getattr(context, "user_id", None))
        or _clean_text(getattr(run, "user_id", None))
        or _clean_text(metadata.get("user_id"))
        or _clean_text(execution_metadata.get("user_id"))
    )
    org_id = (
        _clean_text(getattr(context, "org_id", None))
        or _clean_text(getattr(run, "org_id", None))
        or _clean_text(metadata.get("org_id"))
        or _clean_text(execution_metadata.get("org_id"))
    )
    return actor_id, org_id


def _plan_path(base_path: str | None, relative_path: str) -> str | None:
    if not base_path:
        return None
    normalised = PurePosixPath(str(relative_path or "").replace("\\", "/"))
    if normalised.is_absolute() or any(part in {"", ".", ".."} for part in normalised.parts):
        return None
    base = Path(base_path).expanduser()
    if base.exists() and base.is_file():
        return str(base) if normalised.as_posix() == base.name else None
    return str(base / normalised.as_posix())


def _publish_target(resource: Mapping[str, Any]) -> dict[str, Any]:
    if resource.get("source_path"):
        return {"kind": "local_path", "path": resource.get("source_path")}
    if resource.get("repo"):
        return {"kind": "git_repository", "repo": resource.get("repo")}
    return {"kind": "unknown"}


def _publish_operations(resource: Mapping[str, Any]) -> list[dict[str, Any]]:
    operations: list[dict[str, Any]] = []
    changes = resource.get("changes") if isinstance(resource.get("changes"), dict) else {}
    operation_specs = (
        ("update", "changed_paths"),
        ("create", "new_paths"),
        ("delete", "deleted_paths"),
        ("resolve_conflict", "conflicted_paths"),
    )
    for operation, change_key in operation_specs:
        for path in changes.get(change_key) or []:
            operations.append({
                "operation": operation,
                "path": path,
                "draft_path": _plan_path(resource.get("workspace_path") or resource.get("resource_path"), path),
                "target_path": _plan_path(resource.get("source_path"), path),
            })
    return operations


def _publish_group_status(
    *,
    blocked_reasons: list[str],
    operations: list[dict[str, Any]],
    draft_status: str | None,
) -> str:
    if blocked_reasons:
        return "blocked"
    if operations:
        return "ready"
    if draft_status == "out_of_date":
        return "out_of_date"
    return "clean"


def _publish_blocked_reasons(group: Mapping[str, Any], operations: list[dict[str, Any]]) -> list[str]:
    blocked_reasons: list[str] = []
    if group.get("errors"):
        blocked_reasons.append(f"{group.get('change_source') or 'draft_status'}_failed")
    if any(operation.get("operation") == "resolve_conflict" for operation in operations):
        blocked_reasons.append("conflicted_paths_require_resolution")
    if operations and _as_mapping(group.get("publish_target")).get("kind") == "unknown":
        blocked_reasons.append("publish_target_unavailable")
    publish_target = _as_mapping(group.get("publish_target"))
    if publish_target.get("kind") == "local_path" and any(
        operation.get("operation") in {"create", "update", "delete"}
        and not operation.get("target_path")
        for operation in operations
    ):
        target_path = _clean_text(publish_target.get("path"))
        if target_path and Path(target_path).expanduser().is_file():
            blocked_reasons.append("file_project_root_cannot_publish_additional_paths")
        else:
            blocked_reasons.append("publish_operation_path_unavailable")
    return blocked_reasons


def project_publish_plan_payload(
    *,
    repo_status=None,
    allow_conflict_checkpoint_publish: bool = False,
) -> dict[str, Any]:
    repo_status = repo_status or repo_draft_status
    status_payload = project_draft_status_payload(
        repo_status=repo_status,
        allow_conflict_checkpoint_publish=allow_conflict_checkpoint_publish,
    )
    if not status_payload.get("ok"):
        status_payload["action"] = "plan_publish"
        return status_payload

    groups: list[dict[str, Any]] = []
    for resource in status_payload["resources"]:
        operations = _publish_operations(resource)
        draft_status = _clean_text(resource.get("status"))
        publish_target = _publish_target(resource)
        group_for_blockers = {
            "errors": resource.get("errors"),
            "change_source": resource.get("change_source"),
            "publish_target": publish_target,
        }
        blocked_reasons = _publish_blocked_reasons(group_for_blockers, operations)
        diff_preview = None
        if publish_target.get("kind") == "local_path" and resource.get("workspace_path") and publish_target.get("path"):
            try:
                diff_preview = build_draft_diff(
                    Path(str(publish_target.get("path"))).expanduser(),
                    Path(str(resource.get("workspace_path"))).expanduser(),
                    paths=[str(operation.get("path")) for operation in operations if operation.get("path")],
                    allow_conflict_checkpoint_publish=allow_conflict_checkpoint_publish,
                )
            except Exception as exc:
                diff_preview = {"error": str(exc)}
        groups.append({
            "resource_id": resource.get("id"),
            "mount_path": resource.get("mount_path"),
            "label": resource.get("label"),
            "workspace_path": resource.get("workspace_path"),
            "publish_target": publish_target,
            "status": _publish_group_status(
                blocked_reasons=blocked_reasons,
                operations=operations,
                draft_status=draft_status,
            ),
            "blocked_reasons": blocked_reasons,
            "draft_status": draft_status,
            "change_source": resource.get("change_source"),
            "errors": resource.get("errors") or [],
            "change_counts": resource.get("change_counts"),
            "operations": operations,
            "diff": diff_preview,
        })

    return {
        "ok": True,
        "action": "plan_publish",
        "run_id": status_payload.get("run_id"),
        "idea_id": status_payload.get("idea_id"),
        "mutates_project_root": False,
        "plan_only": True,
        "summary": {
            "resource_count": len(groups),
            "operation_count": sum(len(group["operations"]) for group in groups),
            "blocked_count": sum(1 for group in groups if group["status"] == "blocked"),
        },
        "groups": groups,
    }


def _normalise_publish_path(value: Any) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    return text.replace("\\", "/").removeprefix("./").rstrip("/")


def _operation_path_candidates(operation: Mapping[str, Any], group: Mapping[str, Any]) -> set[str]:
    candidates = {
        _normalise_publish_path(operation.get("path")),
        _normalise_publish_path(operation.get("draft_path")),
        _normalise_publish_path(operation.get("target_path")),
    }
    mount_path = _normalise_publish_path(group.get("mount_path"))
    operation_path = _normalise_publish_path(operation.get("path"))
    if mount_path and operation_path:
        mount = mount_path.strip("/")
        candidates.add(f"{mount}/{operation_path}")
        candidates.add(f"/{mount}/{operation_path}")
    return {candidate for candidate in candidates if candidate}


def _operation_matches_publish_paths(
    operation: Mapping[str, Any],
    group: Mapping[str, Any],
    publish_paths: set[str],
) -> bool:
    candidates = _operation_path_candidates(operation, group)
    stripped_candidates = {candidate.lstrip("/") for candidate in candidates}
    for publish_path in publish_paths:
        if publish_path in candidates or publish_path.lstrip("/") in stripped_candidates:
            return True
    return False


def _publish_resource_matches(group: Mapping[str, Any], resource_id: str) -> bool:
    target = _as_mapping(group.get("publish_target"))
    candidates = {
        str(group.get("resource_id") or ""),
        str(group.get("mount_path") or ""),
        str(group.get("label") or ""),
        str(group.get("workspace_path") or ""),
        str(target.get("path") or ""),
        str(target.get("repo") or ""),
    }
    return resource_id in candidates


def _filter_publish_groups(
    groups: list[Mapping[str, Any]],
    *,
    resource_ids: list[str] | None = None,
    publish_paths: list[str] | None = None,
) -> list[dict[str, Any]]:
    selected_resource_ids = {resource_id for resource_id in (resource_ids or []) if resource_id}
    selected_paths = {
        normalised
        for path in (publish_paths or [])
        if (normalised := _normalise_publish_path(path))
    }
    filtered: list[dict[str, Any]] = []
    for group in groups:
        if selected_resource_ids and not any(
            _publish_resource_matches(group, resource_id)
            for resource_id in selected_resource_ids
        ):
            continue
        operations = [dict(operation) for operation in group.get("operations") or [] if isinstance(operation, Mapping)]
        if selected_paths:
            operations = [
                operation
                for operation in operations
                if _operation_matches_publish_paths(operation, group, selected_paths)
            ]
        blocked_reasons = _publish_blocked_reasons(group, operations)
        draft_status = _clean_text(group.get("draft_status"))
        filtered_group = dict(group)
        filtered_group["operations"] = operations
        filtered_group["blocked_reasons"] = blocked_reasons
        filtered_group["status"] = _publish_group_status(
            blocked_reasons=blocked_reasons,
            operations=operations,
            draft_status=draft_status,
        )
        filtered.append(filtered_group)
    return filtered


def _copy_publish_path(draft_path: str | None, target_path: str | None) -> None:
    if not draft_path or not target_path:
        raise ValueError("publish operation is missing draft_path or target_path")
    draft = Path(draft_path).expanduser()
    target = Path(target_path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    if draft.is_dir():
        if target.exists():
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        shutil.copytree(draft, target)
    else:
        shutil.copy2(draft, target)


def _delete_publish_path(target_path: str | None) -> None:
    if not target_path:
        raise ValueError("publish delete operation is missing target_path")
    target = Path(target_path).expanduser()
    if target.is_dir():
        shutil.rmtree(target)
    elif target.exists() or target.is_symlink():
        target.unlink()


def _version_metadata(
    group: Mapping[str, Any],
    *,
    run_id: str | None,
    idea_id: str | None,
    actor_id: str | None,
    org_id: str | None,
    phase: str,
    operations: list[dict[str, Any]],
) -> dict[str, Any]:
    return build_project_root_version_metadata(
        run_id=run_id,
        idea_id=idea_id,
        actor_id=actor_id,
        org_id=org_id,
        resource_id=group.get("resource_id"),
        mount_path=group.get("mount_path"),
        phase=phase,
        operations=operations,
    )


def _version_dict(version: Any | None) -> dict[str, Any] | None:
    if version is None:
        return None
    if hasattr(version, "to_dict"):
        return version.to_dict()
    if hasattr(version, "__dict__"):
        return dict(version.__dict__)
    if isinstance(version, Mapping):
        return dict(version)
    return {"id": str(version), "version_id": str(version)}


def _refresh_published_draft_paths(source_path: str, workspace_path: str, operations: list[dict[str, Any]]) -> None:
    source = Path(source_path).expanduser()
    draft = Path(workspace_path).expanduser()
    metadata = load_draft_metadata(draft)
    base_manifest = dict(metadata.get("base_manifest") or {})
    root_manifest = build_file_manifest(source)
    if not base_manifest:
        base_manifest = {path: dict(entry) for path, entry in root_manifest.items()}
    for operation in operations:
        relative_path = _clean_text(operation.get("path"))
        if not relative_path:
            continue
        root_entry = root_manifest.get(relative_path)
        if root_entry is None:
            base_manifest.pop(relative_path, None)
        else:
            base_manifest[relative_path] = dict(root_entry)
    published_paths = [_clean_text(operation.get("path")) for operation in operations]
    published_paths = [path for path in published_paths if path]
    refresh_base_snapshots_for_paths(source, draft, published_paths)
    clear_conflict_checkpoints(draft, published_paths)
    metadata = load_draft_metadata(draft)
    save_draft_metadata(draft, metadata, base_manifest=dict(sorted(base_manifest.items())), source_root=str(source))


def _publish_local_group(
    group: Mapping[str, Any],
    *,
    run_id: str | None,
    idea_id: str | None,
    actor_id: str | None,
    org_id: str | None,
    sync_after_publish: bool = True,
) -> dict[str, Any]:
    target = _as_mapping(group.get("publish_target"))
    source_path = _clean_text(target.get("path"))
    workspace_path = _clean_text(group.get("workspace_path"))
    if target.get("kind") != "local_path" or not source_path or not workspace_path:
        return {
            "resource_id": group.get("resource_id"),
            "mount_path": group.get("mount_path"),
            "status": "blocked",
            "blocked_reasons": ["publish_target_unavailable"],
            "operations": [],
        }

    applied: list[dict[str, Any]] = []
    operations = [dict(operation) for operation in group.get("operations") or [] if isinstance(operation, Mapping)]
    before_version = capture_project_root_version(
        Path(source_path).expanduser(),
        label="before-draft-publish",
        metadata=_version_metadata(
            group,
            run_id=run_id,
            idea_id=idea_id,
            actor_id=actor_id,
            org_id=org_id,
            phase="before",
            operations=operations,
        ),
    )
    try:
        for operation in group.get("operations") or []:
            if not isinstance(operation, Mapping):
                continue
            op = str(operation.get("operation") or "")
            if op in {"create", "update"}:
                _copy_publish_path(_clean_text(operation.get("draft_path")), _clean_text(operation.get("target_path")))
            elif op == "delete":
                _delete_publish_path(_clean_text(operation.get("target_path")))
            else:
                raise ValueError("conflicted paths require resolution before publish")
            applied.append(dict(operation))
    except Exception as exc:
        rollback_errors: list[str] = []
        try:
            restore_project_root_version(Path(source_path).expanduser(), before_version.version_id)
            if sync_after_publish:
                sync_draft_from_root(Path(source_path).expanduser(), Path(workspace_path).expanduser())
            else:
                _refresh_published_draft_paths(source_path, workspace_path, applied)
        except Exception as rollback_exc:
            rollback_errors.append(str(rollback_exc))
        blocked_reasons = ["publish_failed_rolled_back"] if not rollback_errors else ["publish_failed_rollback_failed"]
        return {
            "resource_id": group.get("resource_id"),
            "mount_path": group.get("mount_path"),
            "status": "blocked",
            "blocked_reasons": blocked_reasons,
            "operations": applied,
            "error": str(exc),
            "rollback_errors": rollback_errors,
            "root_versions": {
                "before": _version_dict(before_version),
                "after": None,
            },
        }

    after_version = capture_project_root_version(
        Path(source_path).expanduser(),
        label="after-draft-publish",
        metadata=_version_metadata(
            group,
            run_id=run_id,
            idea_id=idea_id,
            actor_id=actor_id,
            org_id=org_id,
            phase="after",
            operations=applied,
        ),
    )
    if sync_after_publish:
        sync_draft_from_root(Path(source_path).expanduser(), Path(workspace_path).expanduser())
    else:
        _refresh_published_draft_paths(source_path, workspace_path, applied)
    return {
        "resource_id": group.get("resource_id"),
        "mount_path": group.get("mount_path"),
        "status": "published",
        "blocked_reasons": [],
        "operations": applied,
        "root_versions": {
            "before": _version_dict(before_version),
            "after": _version_dict(after_version),
        },
    }


def _publish_repo_result_payload(result: Any) -> dict[str, Any]:
    if hasattr(result, "__dict__"):
        payload = dict(result.__dict__)
    elif isinstance(result, Mapping):
        payload = dict(result)
    else:
        payload = {"ok": False, "errors": [str(result)]}
    if "ok" not in payload:
        payload["ok"] = not payload.get("errors")
    return payload


def _publish_repo_group(
    group: Mapping[str, Any],
    *,
    branch_name: str | None,
    commit_message: str | None,
    push: bool,
    create_pr: bool,
    pr_title: str | None,
    pr_body: str | None,
    check_upstream: bool,
    base_branch: str | None,
    selected_paths: list[str] | None = None,
    repo_publish=None,
) -> dict[str, Any]:
    target = _as_mapping(group.get("publish_target"))
    workspace_path = _clean_text(group.get("workspace_path"))
    if target.get("kind") != "git_repository" or not workspace_path:
        return {
            "resource_id": group.get("resource_id"),
            "mount_path": group.get("mount_path"),
            "status": "blocked",
            "blocked_reasons": ["publish_target_unavailable"],
            "operations": [],
        }
    repo_publish = repo_publish or publish_repo_draft
    result = repo_publish(
        Path(workspace_path).expanduser(),
        branch_name=branch_name,
        commit_message=commit_message or "Publish Project draft",
        push=push,
        create_pr=create_pr,
        pr_title=pr_title,
        pr_body=pr_body,
        check_upstream=check_upstream,
        base_branch=base_branch,
        selected_paths=selected_paths,
    )
    payload = _publish_repo_result_payload(result)
    return {
        "resource_id": group.get("resource_id"),
        "mount_path": group.get("mount_path"),
        "status": "published" if payload.get("ok") else "blocked",
        "blocked_reasons": [] if payload.get("ok") else ["repo_publish_failed"],
        "operations": list(group.get("operations") or []),
        "repo_publish": payload,
    }


def _conflict_resolution_guidance(blocked_groups: list[Mapping[str, Any]]) -> dict[str, Any]:
    conflicts: list[dict[str, Any]] = []
    for group in blocked_groups:
        target = _as_mapping(group.get("publish_target"))
        source_path = _clean_text(target.get("path")) if target.get("kind") == "local_path" else None
        workspace_path = _clean_text(group.get("workspace_path"))
        conflict_paths = [
            _clean_text(operation.get("path"))
            for operation in group.get("operations") or []
            if isinstance(operation, Mapping) and operation.get("operation") == "resolve_conflict"
        ]
        conflict_paths = [path for path in conflict_paths if path]
        if target.get("kind") == "local_path" and source_path and workspace_path and conflict_paths:
            record_conflict_checkpoints(
                Path(source_path).expanduser(),
                Path(workspace_path).expanduser(),
                conflict_paths,
            )
        diff_payload = _as_mapping(group.get("diff"))
        for path in conflict_paths:
            conflicts.append({
                "resource_id": group.get("resource_id"),
                "mount_path": group.get("mount_path"),
                "path": path,
                "root_path": _plan_path(source_path, path),
                "draft_path": _plan_path(workspace_path, path),
                "root_to_draft_diff": [
                    item for item in (diff_payload.get("root_to_draft") or [])
                    if isinstance(item, Mapping) and item.get("path") == path
                ],
                "base_to_draft_diff": [
                    item for item in (diff_payload.get("base_to_draft") or [])
                    if isinstance(item, Mapping) and item.get("path") == path
                ],
            })
    return {
        "required": True,
        "instructions": (
            "Publishing is blocked because the Project root changed on the same paths as this thread draft. "
            "Resolve each conflicted draft file against the latest Project root before retrying. Compare root -> draft "
            "to see what would be published, and compare base -> draft to recover the thread's original intent. "
            "Edit the draft so it preserves the user's intent while respecting the current root philosophy, then retry "
            "manage_project(action=\"publish_draft\"). If the current draft already expresses the correct resolution, "
            "retry publish_draft deliberately; the conflict checkpoint will treat that retry as the resolution signal as "
            "long as the root has not changed again."
        ),
        "retry_action": {
            "tool": "manage_project",
            "arguments": {"action": "publish_draft"},
        },
        "conflicts": conflicts,
    }


def _has_conflict_blockers(groups: list[Mapping[str, Any]]) -> bool:
    return any(
        "conflicted_paths_require_resolution" in (group.get("blocked_reasons") or [])
        for group in groups
    )


def project_publish_draft_payload(
    *,
    resource_id: str | None = None,
    resource_ids: list[str] | None = None,
    publish_paths: list[str] | None = None,
    path: str | None = None,
    branch_name: str | None = None,
    commit_message: str | None = None,
    push: bool = False,
    create_pr: bool = False,
    pr_title: str | None = None,
    pr_body: str | None = None,
    check_upstream: bool = True,
    base_branch: str | None = None,
    repo_status=None,
    repo_publish=None,
) -> dict[str, Any]:
    repo_status = repo_status or repo_draft_status
    repo_publish = repo_publish or publish_repo_draft
    plan = project_publish_plan_payload(
        repo_status=repo_status,
        allow_conflict_checkpoint_publish=True,
    )
    if not plan.get("ok"):
        plan["action"] = "publish_draft"
        return plan

    groups = [group for group in plan.get("groups") or [] if isinstance(group, Mapping)]
    selected_resource_ids = _strings_from_value(resource_ids)
    if resource_id:
        selected_resource_ids.append(resource_id)
    selected_publish_paths = _paths_from_value(publish_paths)
    if path:
        selected_publish_paths.append(path)
    has_filters = bool(selected_resource_ids or selected_publish_paths)
    has_path_filters = bool(selected_publish_paths)
    if has_filters:
        groups = _filter_publish_groups(
            groups,
            resource_ids=selected_resource_ids,
            publish_paths=selected_publish_paths,
        )
    blocked = [group for group in groups if group.get("status") == "blocked"]
    if blocked:
        has_conflicts = _has_conflict_blockers(blocked)
        conflict_resolution = _conflict_resolution_guidance(blocked) if has_conflicts else None
        return {
            "ok": False,
            "action": "publish_draft",
            "code": "project_draft_conflicts_require_resolution" if has_conflicts else "project_draft_publish_blocked",
            "error": (
                "Project draft has root conflicts. Resolve the draft against the latest root, then retry publish_draft."
                if has_conflicts
                else "Project draft has blocked resources; resolve blockers before publishing."
            ),
            "mutated_project_root": False,
            "summary": {
                "published_groups": 0,
                "operation_count": 0,
                "blocked_count": len(blocked),
            },
            "blocked_groups": blocked,
            **({"conflict_resolution": conflict_resolution} if conflict_resolution else {}),
        }
    if has_filters and not sum(len(group.get("operations") or []) for group in groups):
        return {
            "ok": False,
            "action": "publish_draft",
            "code": "project_draft_publish_selection_empty",
            "error": "No Project draft changes matched the requested publish filters.",
            "mutated_project_root": False,
            "summary": {
                "published_groups": 0,
                "operation_count": 0,
                "blocked_count": 0,
            },
            "selected_resource_ids": selected_resource_ids,
            "selected_publish_paths": selected_publish_paths,
        }

    published_groups: list[dict[str, Any]] = []
    blocked_groups: list[dict[str, Any]] = []
    actor_id, org_id = _current_publish_actor()
    for group in groups:
        if not group.get("operations"):
            continue
        target = _as_mapping(group.get("publish_target"))
        if target.get("kind") == "git_repository":
            selected_repo_paths = [
                str(operation.get("path"))
                for operation in group.get("operations") or []
                if isinstance(operation, Mapping) and operation.get("path")
            ] if has_path_filters else None
            result = _publish_repo_group(
                group,
                branch_name=branch_name,
                commit_message=commit_message,
                push=push,
                create_pr=create_pr,
                pr_title=pr_title,
                pr_body=pr_body,
                check_upstream=check_upstream,
                base_branch=base_branch,
                selected_paths=selected_repo_paths,
                repo_publish=repo_publish,
            )
        else:
            result = _publish_local_group(
                group,
                run_id=plan.get("run_id"),
                idea_id=plan.get("idea_id"),
                actor_id=actor_id,
                org_id=org_id,
                sync_after_publish=not has_path_filters,
            )
        if result["status"] == "blocked":
            blocked_groups.append(result)
        else:
            published_groups.append(result)

    if blocked_groups:
        return {
            "ok": False,
            "action": "publish_draft",
            "code": "project_draft_publish_blocked",
            "error": "Some Project draft resources do not have a supported publish adapter.",
            "mutated_project_root": bool(published_groups),
            "summary": {
                "published_groups": len(published_groups),
                "operation_count": sum(len(group["operations"]) for group in published_groups),
                "blocked_count": len(blocked_groups),
            },
            "published_groups": published_groups,
            "blocked_groups": blocked_groups,
        }

    return {
        "ok": True,
        "action": "publish_draft",
        "mutated_project_root": bool(published_groups),
        "summary": {
            "published_groups": len(published_groups),
            "operation_count": sum(len(group["operations"]) for group in published_groups),
            "blocked_count": 0,
        },
        "published_groups": published_groups,
    }


__all__ = [
    "project_publish_plan_payload",
    "project_publish_draft_payload",
]
