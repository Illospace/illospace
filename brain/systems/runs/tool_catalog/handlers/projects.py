"""Project Context management tool handlers."""

from __future__ import annotations

from typing import Any

from brain.systems.cortex.project_context.draft_state import (
    project_draft_status_payload,
    project_refresh_draft_from_root_payload,
)
from brain.systems.cortex.project_context.publish import (
    project_publish_draft_payload,
    project_publish_plan_payload,
)
from brain.systems.cortex.project_context.root_history import (
    project_preview_root_version_payload,
    project_restore_root_version_payload,
    project_root_versions_payload,
)
from brain.systems.runs.tool_catalog.handlers.common import *


PROJECT_DRAFT_OPERATIONS: dict[str, dict[str, object]] = {
    "draft_status": {
        "required": ["current Project-backed AgentRun/thread"],
        "optional": [],
        "effect": "read Project draft workspace status without mutating Project roots",
    },
    "plan_publish": {
        "required": ["current Project-backed AgentRun/thread"],
        "optional": [],
        "effect": "produce a grouped publish plan for Project draft changes without writing files",
    },
    "refresh_draft_from_root": {
        "required": ["current Project-backed AgentRun/thread"],
        "optional": ["resource_id", "resource_ids"],
        "effect": "explicitly refresh the thread draft from the latest Project root without mutating the root",
    },
    "publish_draft": {
        "required": ["current Project-backed AgentRun/thread"],
        "optional": ["resource_ids", "publish_paths", "path", "acknowledge_conflict_resolution"],
        "effect": "publish local Project draft changes back to root, blocking with conflict-resolution guidance when root and draft changed the same paths",
    },
    "root_versions": {
        "required": ["current Project-backed AgentRun/thread"],
        "optional": ["resource_id"],
        "effect": "list local Project root versions captured for attached draft resources",
    },
    "preview_root_version": {
        "required": ["current Project-backed AgentRun/thread", "version_id"],
        "optional": ["resource_id"],
        "effect": "preview how restoring a captured local Project root version would change the root",
    },
    "restore_root_version": {
        "required": ["current Project-backed AgentRun/thread", "version_id"],
        "optional": ["resource_id"],
        "effect": "restore a local Project root to a captured version and refresh its thread draft",
    },
}


def _project_manage_tool_guide(operation: str | None = None) -> str:
    requested = str(operation or "").strip().lower()
    if requested in PROJECT_DRAFT_OPERATIONS:
        return json.dumps(
            {"tool": "manage_project", "operation": requested, **PROJECT_DRAFT_OPERATIONS[requested]},
            default=str,
        )
    payload = json.loads(_manage_tool_guide("manage_project", operation))
    if requested:
        return json.dumps(payload, default=str)
    operations = dict(payload.get("operations") or {})
    operations.update(PROJECT_DRAFT_OPERATIONS)
    payload["operations"] = operations
    return json.dumps(payload, default=str)


def _project_tool_context() -> tuple[str | None, str | None, str | None]:
    execution_metadata = getattr(_agent_context, "execution_metadata", {}) or {}
    org_id = getattr(_agent_context, "org_id", None) or execution_metadata.get("org_id")
    user_id = getattr(_agent_context, "user_id", None) or execution_metadata.get("user_id")
    idea_id = getattr(_agent_context, "idea_id", None)
    return str(org_id) if org_id else None, str(user_id) if user_id else None, str(idea_id) if idea_id else None


def _profile_read(profile) -> dict[str, Any]:
    from brain.systems.cortex.project_context.profiles import profile_to_read

    return profile_to_read(profile).model_dump(mode="json", by_alias=True)


def _attachment_read(attachment) -> dict[str, Any]:
    from brain.systems.cortex.project_context.profiles import attachment_to_read

    return attachment_to_read(attachment).model_dump(mode="json", by_alias=True)


def _profile_stmt(org_id: str, user_id: str | None, *, include_inactive: bool = False):
    from sqlalchemy import select
    from brain.platform.db.models.idea import ProjectProfile, ProjectProfileAccess
    from brain.systems.cortex.project_context.access import project_profile_visible_predicate

    stmt = select(ProjectProfile).where(ProjectProfile.org_id == org_id)
    stmt = stmt.where(project_profile_visible_predicate(ProjectProfile, ProjectProfileAccess, user_id))
    if not include_inactive:
        stmt = stmt.where(ProjectProfile.active.is_(True))
    return stmt


async def _get_profile(session, org_id: str, user_id: str | None, profile_id: str, *, include_inactive: bool = False):
    from brain.platform.db.models.idea import ProjectProfile

    profile = await session.scalar(
        _profile_stmt(org_id, user_id, include_inactive=include_inactive)
        .where(ProjectProfile.id == profile_id)
    )
    if profile is None:
        raise ValueError("Project profile not found")
    return profile


def _require_manage_access(profile, actor: dict[str, Any]) -> None:
    from brain.systems.cortex.project_context.access import can_manage_project_profile

    if not can_manage_project_profile(profile, actor):
        raise ValueError("Only the project owner can change this project")


def _validated_project_context(project_context: dict[str, Any]) -> dict[str, Any]:
    from brain.systems.cortex.project_context.snapshot import (
        ProjectContextValidationError,
        validated_project_context_snapshot,
    )

    try:
        return validated_project_context_snapshot(project_context)
    except ProjectContextValidationError as exc:
        raise ValueError(str(exc)) from exc


def _profile_project_context(profile, project_context: dict[str, Any] | None = None) -> dict[str, Any]:
    from brain.systems.cortex.project_context.identity import stamped_project_context

    return _validated_project_context(stamped_project_context(profile, project_context))


def _context_from_inputs(
    *,
    project_context: dict[str, Any] | None = None,
    resources: list[dict[str, Any]] | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    from brain.systems.cortex.project_context.resources import normalize_project_resource

    if isinstance(project_context, dict):
        context = dict(project_context)
    else:
        context = {
            "version": 1,
            "source": "manage_project",
            "selected_profile_name": name or "Project",
            "validation_status": "client_validated",
            "resources": [],
        }
    if resources is not None:
        context["resources"] = [
            normalize_project_resource(resource, index=index)
            for index, resource in enumerate(resources)
            if isinstance(resource, dict)
        ]
    return _validated_project_context(context)


def _project_resources(profile) -> list[dict[str, Any]]:
    context = profile.project_context if isinstance(profile.project_context, dict) else {}
    return [dict(item) for item in (context.get("resources") or []) if isinstance(item, dict)]


def _store_resources(profile, resources: list[dict[str, Any]]) -> None:
    context = dict(profile.project_context or {})
    context["resources"] = resources
    profile.project_context = _profile_project_context(profile, context)


def _unique_resource_id(resource: dict[str, Any], existing_ids: set[str], index: int) -> str:
    base = str(resource.get("id") or resource.get("name") or resource.get("label") or f"resource-{index + 1}").strip()
    base = base or f"resource-{index + 1}"
    candidate = base
    suffix = 2
    while candidate in existing_ids:
        candidate = f"{base}-{suffix}"
        suffix += 1
    existing_ids.add(candidate)
    return candidate


def _resource_matches(resource: dict[str, Any], resource_id: str) -> bool:
    if str(resource.get("id") or "") == resource_id:
        return True
    for key in ("path", "uri", "repo", "name", "label"):
        value = resource.get(key)
        if isinstance(value, str) and value.strip() == resource_id:
            return True
    return False


async def _handle_manage_project(
    action: str,
    operation: str | None = None,
    project_id: str | None = None,
    slug: str | None = None,
    name: str | None = None,
    description: str | None = None,
    project_context: dict | None = None,
    resources: list[dict] | None = None,
    resource: dict | None = None,
    resource_id: str | None = None,
    resource_ids: list[str] | None = None,
    metadata: dict | None = None,
    visibility: str | None = None,
    shared_usernames: list[str] | None = None,
    default_environment_binding_id: int | None = None,
    environment_binding_id: int | None = None,
    idea_id: str | None = None,
    publish_paths: list[str] | None = None,
    path: str | None = None,
    version_id: str | None = None,
    branch_name: str | None = None,
    commit_message: str | None = None,
    push: bool = False,
    create_pr: bool = False,
    pr_title: str | None = None,
    pr_body: str | None = None,
    check_upstream: bool = True,
    base_branch: str | None = None,
    acknowledge_conflict_resolution: bool = False,
    include_inactive: bool = False,
) -> str:
    action = str(action or "").strip().lower()
    if action in {"help", "schema"}:
        return _project_manage_tool_guide(operation)

    if action == "draft_status":
        return json.dumps(project_draft_status_payload(), default=str)
    if action == "plan_publish":
        return json.dumps(project_publish_plan_payload(), default=str)
    if action == "refresh_draft_from_root":
        return json.dumps(
            project_refresh_draft_from_root_payload(resource_id=resource_id, resource_ids=resource_ids),
            default=str,
        )
    if action == "publish_draft":
        return json.dumps(
            project_publish_draft_payload(
                resource_id=resource_id,
                resource_ids=resource_ids,
                publish_paths=publish_paths,
                path=path,
                branch_name=branch_name,
                commit_message=commit_message,
                push=push,
                create_pr=create_pr,
                pr_title=pr_title,
                pr_body=pr_body,
                check_upstream=check_upstream,
                base_branch=base_branch,
                acknowledge_conflict_resolution=acknowledge_conflict_resolution,
            ),
            default=str,
        )
    if action == "root_versions":
        return json.dumps(project_root_versions_payload(resource_id=resource_id), default=str)
    if action == "preview_root_version":
        return json.dumps(
            project_preview_root_version_payload(version_id=version_id, resource_id=resource_id),
            default=str,
        )
    if action == "restore_root_version":
        return json.dumps(
            project_restore_root_version_payload(version_id=version_id, resource_id=resource_id),
            default=str,
        )

    from sqlalchemy import select

    from brain.systems.cortex.project_context.access import (
        normalize_project_visibility,
        require_idea_for_project_actor,
        sync_project_access_list,
    )
    from brain.systems.cortex.project_context.resources import normalize_project_resource
    from brain.systems.cortex.project_context.snapshot import (
        ProjectContextValidationError,
        validated_project_context_snapshot,
    )
    from brain.platform.db.models.idea import IdeaProjectAttachment, ProjectProfile
    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    org_id, user_id, context_idea_id = _project_tool_context()
    if not org_id:
        return json.dumps({"error": "manage_project requires an org-scoped run"})
    actor = {
        "id": user_id,
        "org_id": org_id,
        "role": "owner",
        "principal_type": "human",
    }
    selected_project_id = project_id

    try:
        async with UnitOfWork() as uow:
            if action == "list":
                stmt = _profile_stmt(org_id, user_id, include_inactive=include_inactive).order_by(ProjectProfile.created_at.desc())
                profiles = (await uow.session.scalars(stmt)).all()
                return json.dumps({"projects": [_profile_read(profile) for profile in profiles]}, default=str)

            if action == "get":
                if not selected_project_id:
                    return json.dumps({"error": "get requires: project_id"})
                profile = await _get_profile(uow.session, org_id, user_id, selected_project_id, include_inactive=include_inactive)
                return json.dumps({"project": _profile_read(profile)}, default=str)

            if action == "create":
                if not slug or not name:
                    return json.dumps({"error": "create requires: slug, name"})
                existing = await uow.session.scalar(
                    select(ProjectProfile).where(ProjectProfile.org_id == org_id, ProjectProfile.slug == slug)
                )
                if existing is not None:
                    return json.dumps({"error": "Project profile slug already exists"})
                context = _context_from_inputs(project_context=project_context, resources=resources, name=name)
                profile = ProjectProfile(
                    org_id=org_id,
                    user_id=user_id,
                    slug=slug,
                    name=name,
                    description=description,
                    project_context=context,
                    visibility=normalize_project_visibility(visibility),
                    default_environment_binding_id=default_environment_binding_id,
                    metadata_=metadata or {},
                )
                uow.session.add(profile)
                await uow.session.flush()
                profile.project_context = _profile_project_context(profile, context)
                await sync_project_access_list(
                    uow.session,
                    profile,
                    org_id=org_id,
                    shared_usernames=shared_usernames,
                    actor_user_id=user_id,
                )
                await uow.commit()
                return json.dumps({"project": _profile_read(profile)}, default=str)

            if action == "update":
                if not selected_project_id:
                    return json.dumps({"error": "update requires: project_id"})
                profile = await _get_profile(uow.session, org_id, user_id, selected_project_id, include_inactive=True)
                _require_manage_access(profile, actor)
                if slug and slug != profile.slug:
                    existing = await uow.session.scalar(
                        select(ProjectProfile).where(
                            ProjectProfile.org_id == org_id,
                            ProjectProfile.slug == slug,
                            ProjectProfile.id != profile.id,
                        )
                    )
                    if existing is not None:
                        return json.dumps({"error": "Project profile slug already exists"})
                    profile.slug = slug
                if name is not None:
                    profile.name = name
                if description is not None:
                    profile.description = description
                if project_context is not None or resources is not None:
                    profile.project_context = _context_from_inputs(
                        project_context=project_context or profile.project_context,
                        resources=resources,
                        name=profile.name,
                    )
                if visibility is not None:
                    profile.visibility = normalize_project_visibility(visibility)
                if shared_usernames is not None:
                    await sync_project_access_list(
                        uow.session,
                        profile,
                        org_id=org_id,
                        shared_usernames=shared_usernames,
                        actor_user_id=user_id,
                    )
                if default_environment_binding_id is not None:
                    profile.default_environment_binding_id = default_environment_binding_id
                if metadata is not None:
                    profile.metadata_ = metadata
                profile.project_context = _profile_project_context(profile)
                uow.session.add(profile)
                await uow.commit()
                return json.dumps({"project": _profile_read(profile)}, default=str)

            if action in {"archive", "delete"}:
                if not selected_project_id:
                    return json.dumps({"error": f"{action} requires: project_id"})
                profile = await _get_profile(uow.session, org_id, user_id, selected_project_id, include_inactive=True)
                _require_manage_access(profile, actor)
                profile.active = False
                uow.session.add(profile)
                await uow.commit()
                return json.dumps({"project": _profile_read(profile), "archived": True}, default=str)

            if action == "add_resource":
                if not selected_project_id:
                    return json.dumps({"error": "add_resource requires: project_id"})
                incoming = resources or ([resource] if isinstance(resource, dict) else [])
                if not incoming:
                    return json.dumps({"error": "add_resource requires: resource or resources"})
                profile = await _get_profile(uow.session, org_id, user_id, selected_project_id, include_inactive=True)
                _require_manage_access(profile, actor)
                current = _project_resources(profile)
                existing_ids = {str(item.get("id")) for item in current if item.get("id")}
                for raw in incoming:
                    normalized = normalize_project_resource(raw, index=len(current))
                    normalized["id"] = _unique_resource_id(normalized, existing_ids, len(current))
                    current.append(normalized)
                _store_resources(profile, current)
                uow.session.add(profile)
                await uow.commit()
                return json.dumps({"project": _profile_read(profile)}, default=str)

            if action == "update_resource":
                if not selected_project_id or not resource_id or not isinstance(resource, dict):
                    return json.dumps({"error": "update_resource requires: project_id, resource_id, resource"})
                profile = await _get_profile(uow.session, org_id, user_id, selected_project_id, include_inactive=True)
                _require_manage_access(profile, actor)
                current = _project_resources(profile)
                for index, existing in enumerate(current):
                    if _resource_matches(existing, resource_id):
                        raw = {**existing, **resource}
                        raw.setdefault("id", existing.get("id") or resource_id)
                        current[index] = normalize_project_resource(raw, index=index)
                        break
                else:
                    return json.dumps({"error": "Project resource not found"})
                _store_resources(profile, current)
                uow.session.add(profile)
                await uow.commit()
                return json.dumps({"project": _profile_read(profile)}, default=str)

            if action == "remove_resource":
                if not selected_project_id or not resource_id:
                    return json.dumps({"error": "remove_resource requires: project_id, resource_id"})
                profile = await _get_profile(uow.session, org_id, user_id, selected_project_id, include_inactive=True)
                _require_manage_access(profile, actor)
                current = _project_resources(profile)
                next_resources = [item for item in current if not _resource_matches(item, resource_id)]
                if len(next_resources) == len(current):
                    return json.dumps({"error": "Project resource not found"})
                _store_resources(profile, next_resources)
                uow.session.add(profile)
                await uow.commit()
                return json.dumps({"project": _profile_read(profile)}, default=str)

            if action == "reorder_resources":
                if not selected_project_id or not resource_ids:
                    return json.dumps({"error": "reorder_resources requires: project_id, resource_ids"})
                profile = await _get_profile(uow.session, org_id, user_id, selected_project_id, include_inactive=True)
                _require_manage_access(profile, actor)
                current = _project_resources(profile)
                by_id = {str(item.get("id")): item for item in current if item.get("id")}
                requested = [str(item) for item in resource_ids]
                if len(requested) != len(by_id) or len(set(requested)) != len(requested) or set(requested) != set(by_id):
                    return json.dumps({"error": "resource_ids must include every project resource id exactly once"})
                _store_resources(profile, [by_id[item] for item in requested])
                uow.session.add(profile)
                await uow.commit()
                return json.dumps({"project": _profile_read(profile)}, default=str)

            if action == "attach_to_thread":
                target_idea_id = idea_id or context_idea_id
                if not target_idea_id:
                    return json.dumps({"error": "attach_to_thread requires: idea_id when no Cortex thread is bound"})
                await require_idea_for_project_actor(uow.session, target_idea_id, actor)
                profile = None
                context = project_context
                if selected_project_id:
                    profile = await _get_profile(uow.session, org_id, user_id, selected_project_id)
                    context = _profile_project_context(profile)
                if not context:
                    return json.dumps({"error": "attach_to_thread requires: project_id or project_context"})
                try:
                    snapshot = validated_project_context_snapshot(context)
                except ProjectContextValidationError as exc:
                    return json.dumps({"error": "Invalid project context", "validation_errors": exc.errors})
                attachment = IdeaProjectAttachment(
                    idea_id=target_idea_id,
                    project_profile_id=profile.id if profile else selected_project_id,
                    attached_by=user_id,
                    snapshot=snapshot,
                    permission_scope=snapshot.get("permission_scope") or {},
                    status=str(snapshot.get("status") or "validated"),
                    validation_errors=snapshot.get("validation_errors") or [],
                    environment_binding_id=environment_binding_id
                    if environment_binding_id is not None
                    else (profile.default_environment_binding_id if profile else None),
                    metadata_=metadata or {},
                )
                uow.session.add(attachment)
                await uow.commit()
                return json.dumps({"attachment": _attachment_read(attachment)}, default=str)

            return json.dumps({"error": f"Unknown action: {action}"})
    except Exception as exc:
        logger.exception("manage_project failed: %s", exc)
        return json.dumps({"error": str(exc)})


__all__ = [name for name in globals() if not name.startswith("__")]
