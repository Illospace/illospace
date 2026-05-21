"""List, preview, and restore local Project root versions."""
from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from brain.systems.cortex.project_context.draft_state import project_draft_status_payload
from brain.systems.cortex.project_context.drafts import sync_draft_from_root
from brain.systems.cortex.project_context.versioning import (
    build_project_root_version_metadata,
    capture_project_root_version,
    compare_project_root_to_version,
    list_project_root_versions,
    restore_project_root_version,
    summarize_versions,
)
from brain.systems.runs.execution_context import current_agent_context


def _clean_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


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


def _version_summary_dict(version: Any | None) -> dict[str, Any] | None:
    if version is None:
        return None
    if hasattr(version, "to_summary_dict"):
        return version.to_summary_dict()
    return _version_dict(version)


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _current_actor() -> tuple[str | None, str | None]:
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


def _restore_version_metadata(
    resource: Mapping[str, Any],
    *,
    run_id: Any,
    idea_id: Any,
    actor_id: Any,
    org_id: Any,
    phase: str,
    version_id: str,
) -> dict[str, Any]:
    return build_project_root_version_metadata(
        run_id=run_id,
        idea_id=idea_id,
        actor_id=actor_id,
        org_id=org_id,
        resource_id=resource.get("id"),
        mount_path=resource.get("mount_path"),
        phase=phase,
        event_type="project_root_restore",
        operations=[{"operation": "restore", "version_id": version_id}],
    )


def _local_version_resources(status_payload: Mapping[str, Any], resource_id: str | None = None) -> list[dict[str, Any]]:
    selected = _clean_text(resource_id)
    resources: list[dict[str, Any]] = []
    for resource in status_payload.get("resources") or []:
        if not isinstance(resource, Mapping):
            continue
        source_path = _clean_text(resource.get("source_path"))
        if not source_path:
            continue
        if selected and selected not in {
            str(resource.get("id") or ""),
            str(resource.get("mount_path") or ""),
            str(resource.get("label") or ""),
            source_path,
        }:
            continue
        resources.append(dict(resource))
    return resources


def project_root_versions_payload(resource_id: str | None = None) -> dict[str, Any]:
    status_payload = project_draft_status_payload()
    if not status_payload.get("ok"):
        status_payload["action"] = "root_versions"
        return status_payload

    groups: list[dict[str, Any]] = []
    for resource in _local_version_resources(status_payload, resource_id):
        source_path = _clean_text(resource.get("source_path"))
        versions = list_project_root_versions(Path(source_path).expanduser()) if source_path else []
        version_history = summarize_versions(versions)
        groups.append({
            "resource_id": resource.get("id"),
            "mount_path": resource.get("mount_path"),
            "label": resource.get("label"),
            "source_path": source_path,
            "workspace_path": resource.get("workspace_path"),
            "versions": [_version_summary_dict(version) for version in versions],
            "history": version_history.get("summary"),
        })

    return {
        "ok": True,
        "action": "root_versions",
        "run_id": status_payload.get("run_id"),
        "idea_id": status_payload.get("idea_id"),
        "groups": groups,
        "summary": {
            "resource_count": len(groups),
            "version_count": sum(len(group["versions"]) for group in groups),
        },
    }


def _selected_local_version_resource(
    *,
    action: str,
    resource_id: str | None,
    status_payload: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if not status_payload.get("ok"):
        status_payload["action"] = action
        status_payload["mutated_project_root"] = False
        return None, status_payload
    resources = _local_version_resources(status_payload, resource_id)
    if not resources:
        return None, {
            "ok": False,
            "action": action,
            "code": "project_root_not_found",
            "error": f"No matching local Project root was found for {action}.",
            "mutated_project_root": False,
        }
    if len(resources) > 1 and not _clean_text(resource_id):
        return None, {
            "ok": False,
            "action": action,
            "code": "resource_id_required",
            "error": "Multiple local Project roots are attached; provide resource_id.",
            "mutated_project_root": False,
            "resources": [
                {
                    "resource_id": resource.get("id"),
                    "mount_path": resource.get("mount_path"),
                    "source_path": resource.get("source_path"),
                }
                for resource in resources
            ],
        }
    return resources[0], None


def project_preview_root_version_payload(
    *,
    version_id: str | None,
    resource_id: str | None = None,
) -> dict[str, Any]:
    selected_version = _clean_text(version_id)
    if not selected_version:
        return {
            "ok": False,
            "action": "preview_root_version",
            "code": "version_id_required",
            "error": "preview_root_version requires version_id.",
            "mutated_project_root": False,
        }

    status_payload = project_draft_status_payload()
    resource, error = _selected_local_version_resource(
        action="preview_root_version",
        resource_id=resource_id,
        status_payload=status_payload,
    )
    if error:
        return error
    source_path = _clean_text((resource or {}).get("source_path"))
    if not source_path:
        return {
            "ok": False,
            "action": "preview_root_version",
            "code": "project_root_not_found",
            "error": "The selected Project resource has no local root to preview.",
            "mutated_project_root": False,
        }

    comparison = compare_project_root_to_version(Path(source_path).expanduser(), selected_version)
    return {
        "ok": True,
        "action": "preview_root_version",
        "mutated_project_root": False,
        "resource_id": resource.get("id") if resource else None,
        "mount_path": resource.get("mount_path") if resource else None,
        "source_path": source_path,
        "workspace_path": resource.get("workspace_path") if resource else None,
        "comparison": comparison.to_dict(),
    }


def project_restore_root_version_payload(
    *,
    version_id: str | None,
    resource_id: str | None = None,
) -> dict[str, Any]:
    selected_version = _clean_text(version_id)
    if not selected_version:
        return {
            "ok": False,
            "action": "restore_root_version",
            "code": "version_id_required",
            "error": "restore_root_version requires version_id.",
            "mutated_project_root": False,
        }

    status_payload = project_draft_status_payload()
    resource, error = _selected_local_version_resource(
        action="restore_root_version",
        resource_id=resource_id,
        status_payload=status_payload,
    )
    if error:
        return error
    source_path = _clean_text(resource.get("source_path"))
    workspace_path = _clean_text(resource.get("workspace_path"))
    if not source_path:
        return {
            "ok": False,
            "action": "restore_root_version",
            "code": "project_root_not_found",
            "error": "The selected Project resource has no local root to restore.",
            "mutated_project_root": False,
        }

    actor_id, org_id = _current_actor()
    before_version = capture_project_root_version(
        Path(source_path).expanduser(),
        label="before-root-restore",
        metadata=_restore_version_metadata(
            resource,
            run_id=status_payload.get("run_id"),
            idea_id=status_payload.get("idea_id"),
            actor_id=actor_id,
            org_id=org_id,
            phase="before",
            version_id=selected_version,
        ),
    )
    restored = restore_project_root_version(Path(source_path).expanduser(), selected_version)
    after_version = capture_project_root_version(
        Path(source_path).expanduser(),
        label="after-root-restore",
        metadata=_restore_version_metadata(
            resource,
            run_id=status_payload.get("run_id"),
            idea_id=status_payload.get("idea_id"),
            actor_id=actor_id,
            org_id=org_id,
            phase="after",
            version_id=selected_version,
        ),
    )
    if workspace_path:
        sync_draft_from_root(Path(source_path).expanduser(), Path(workspace_path).expanduser())
    return {
        "ok": True,
        "action": "restore_root_version",
        "mutated_project_root": True,
        "resource_id": resource.get("id"),
        "mount_path": resource.get("mount_path"),
        "source_path": source_path,
        "workspace_path": workspace_path,
        "restored_version": _version_dict(restored),
        "root_versions": {
            "before": _version_dict(before_version),
            "after": _version_dict(after_version),
        },
    }


__all__ = [
    "project_root_versions_payload",
    "project_preview_root_version_payload",
    "project_restore_root_version_payload",
]
