"""Project Context management tool handlers."""

from __future__ import annotations

from typing import Any

from brain.systems.runs.tool_catalog.handlers.common import *


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


def _profile_stmt(org_id: str, *, include_inactive: bool = False):
    from sqlalchemy import select
    from brain.platform.db.models.idea import ProjectProfile

    stmt = select(ProjectProfile).where(ProjectProfile.org_id == org_id)
    if not include_inactive:
        stmt = stmt.where(ProjectProfile.active.is_(True))
    return stmt


def _get_profile(session, org_id: str, profile_id: str, *, include_inactive: bool = False):
    from brain.platform.db.models.idea import ProjectProfile

    profile = session.get(ProjectProfile, profile_id)
    if profile is None or str(profile.org_id or "") != str(org_id):
        raise ValueError("Project profile not found")
    if not include_inactive and profile.active is False:
        raise ValueError("Project profile not found")
    return profile


def _validate_project_context(project_context: dict[str, Any]) -> dict[str, Any]:
    from brain.systems.cortex.project_context.snapshot import snapshot_from_project_context

    snapshot = snapshot_from_project_context(project_context)
    if snapshot.get("status") == "invalid":
        raise ValueError("Invalid project context: " + "; ".join(snapshot.get("validation_errors") or []))
    return project_context


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
    return _validate_project_context(context)


def _project_resources(profile) -> list[dict[str, Any]]:
    context = profile.project_context if isinstance(profile.project_context, dict) else {}
    return [dict(item) for item in (context.get("resources") or []) if isinstance(item, dict)]


def _store_resources(profile, resources: list[dict[str, Any]]) -> None:
    context = dict(profile.project_context or {})
    context["resources"] = resources
    profile.project_context = _validate_project_context(context)


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


def _handle_manage_project(
    action: str,
    project_id: str | None = None,
    profile_id: str | None = None,
    slug: str | None = None,
    name: str | None = None,
    description: str | None = None,
    project_context: dict | None = None,
    resources: list[dict] | None = None,
    resource: dict | None = None,
    resource_id: str | None = None,
    resource_ids: list[str] | None = None,
    metadata: dict | None = None,
    default_environment_binding_id: int | None = None,
    environment_binding_id: int | None = None,
    idea_id: str | None = None,
    include_inactive: bool = False,
) -> str:
    from sqlalchemy import select

    from brain.app.api.routers.cortex._helpers import _require_idea_for_user
    from brain.systems.cortex.project_context.permissions import derive_project_permission_scope
    from brain.systems.cortex.project_context.resources import normalize_project_resource
    from brain.systems.cortex.project_context.snapshot import snapshot_from_project_context
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
    selected_profile_id = profile_id or project_id

    try:
        with UnitOfWork() as uow:
            if action == "list":
                stmt = _profile_stmt(org_id, include_inactive=include_inactive).order_by(ProjectProfile.created_at.desc())
                return json.dumps({"projects": [_profile_read(profile) for profile in uow.session.scalars(stmt).all()]}, default=str)

            if action == "get":
                if not selected_profile_id:
                    return json.dumps({"error": "get requires: project_id"})
                profile = _get_profile(uow.session, org_id, selected_profile_id, include_inactive=include_inactive)
                return json.dumps({"project": _profile_read(profile)}, default=str)

            if action == "create":
                if not slug or not name:
                    return json.dumps({"error": "create requires: slug, name"})
                existing = uow.session.scalar(_profile_stmt(org_id, include_inactive=True).where(ProjectProfile.slug == slug))
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
                    default_environment_binding_id=default_environment_binding_id,
                    metadata_=metadata or {},
                )
                uow.session.add(profile)
                uow.commit()
                return json.dumps({"project": _profile_read(profile)}, default=str)

            if action == "update":
                if not selected_profile_id:
                    return json.dumps({"error": "update requires: project_id"})
                profile = _get_profile(uow.session, org_id, selected_profile_id, include_inactive=True)
                if slug and slug != profile.slug:
                    existing = uow.session.scalar(
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
                if default_environment_binding_id is not None:
                    profile.default_environment_binding_id = default_environment_binding_id
                if metadata is not None:
                    profile.metadata_ = metadata
                uow.session.add(profile)
                uow.commit()
                return json.dumps({"project": _profile_read(profile)}, default=str)

            if action in {"archive", "delete"}:
                if not selected_profile_id:
                    return json.dumps({"error": f"{action} requires: project_id"})
                profile = _get_profile(uow.session, org_id, selected_profile_id, include_inactive=True)
                profile.active = False
                uow.session.add(profile)
                uow.commit()
                return json.dumps({"project": _profile_read(profile), "archived": True}, default=str)

            if action == "add_resource":
                if not selected_profile_id:
                    return json.dumps({"error": "add_resource requires: project_id"})
                incoming = resources or ([resource] if isinstance(resource, dict) else [])
                if not incoming:
                    return json.dumps({"error": "add_resource requires: resource or resources"})
                profile = _get_profile(uow.session, org_id, selected_profile_id, include_inactive=True)
                current = _project_resources(profile)
                existing_ids = {str(item.get("id")) for item in current if item.get("id")}
                for raw in incoming:
                    normalized = normalize_project_resource(raw, index=len(current))
                    normalized["id"] = _unique_resource_id(normalized, existing_ids, len(current))
                    current.append(normalized)
                _store_resources(profile, current)
                uow.session.add(profile)
                uow.commit()
                return json.dumps({"project": _profile_read(profile)}, default=str)

            if action == "update_resource":
                if not selected_profile_id or not resource_id or not isinstance(resource, dict):
                    return json.dumps({"error": "update_resource requires: project_id, resource_id, resource"})
                profile = _get_profile(uow.session, org_id, selected_profile_id, include_inactive=True)
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
                uow.commit()
                return json.dumps({"project": _profile_read(profile)}, default=str)

            if action == "remove_resource":
                if not selected_profile_id or not resource_id:
                    return json.dumps({"error": "remove_resource requires: project_id, resource_id"})
                profile = _get_profile(uow.session, org_id, selected_profile_id, include_inactive=True)
                current = _project_resources(profile)
                next_resources = [item for item in current if not _resource_matches(item, resource_id)]
                if len(next_resources) == len(current):
                    return json.dumps({"error": "Project resource not found"})
                _store_resources(profile, next_resources)
                uow.session.add(profile)
                uow.commit()
                return json.dumps({"project": _profile_read(profile)}, default=str)

            if action == "reorder_resources":
                if not selected_profile_id or not resource_ids:
                    return json.dumps({"error": "reorder_resources requires: project_id, resource_ids"})
                profile = _get_profile(uow.session, org_id, selected_profile_id, include_inactive=True)
                current = _project_resources(profile)
                by_id = {str(item.get("id")): item for item in current if item.get("id")}
                requested = [str(item) for item in resource_ids]
                if len(requested) != len(by_id) or len(set(requested)) != len(requested) or set(requested) != set(by_id):
                    return json.dumps({"error": "resource_ids must include every project resource id exactly once"})
                _store_resources(profile, [by_id[item] for item in requested])
                uow.session.add(profile)
                uow.commit()
                return json.dumps({"project": _profile_read(profile)}, default=str)

            if action == "attach_to_thread":
                target_idea_id = idea_id or context_idea_id
                if not target_idea_id:
                    return json.dumps({"error": "attach_to_thread requires: idea_id when no Cortex thread is bound"})
                _require_idea_for_user(uow.session, target_idea_id, actor)
                profile = None
                context = project_context
                if selected_profile_id:
                    profile = _get_profile(uow.session, org_id, selected_profile_id)
                    context = dict(profile.project_context or {})
                if not context:
                    return json.dumps({"error": "attach_to_thread requires: project_id or project_context"})
                snapshot = snapshot_from_project_context(context)
                if snapshot.get("status") == "invalid":
                    return json.dumps({"error": "Invalid project context", "validation_errors": snapshot.get("validation_errors") or []})
                permission_scope = snapshot.get("permission_scope") or derive_project_permission_scope(snapshot)
                attachment = IdeaProjectAttachment(
                    idea_id=target_idea_id,
                    project_profile_id=profile.id if profile else selected_profile_id,
                    attached_by=user_id,
                    snapshot=snapshot,
                    permission_scope=permission_scope,
                    status=str(snapshot.get("status") or "validated"),
                    validation_errors=snapshot.get("validation_errors") or [],
                    environment_binding_id=environment_binding_id
                    if environment_binding_id is not None
                    else (profile.default_environment_binding_id if profile else None),
                    metadata_=metadata or {},
                )
                uow.session.add(attachment)
                uow.commit()
                return json.dumps({"attachment": _attachment_read(attachment)}, default=str)

            return json.dumps({"error": f"Unknown action: {action}"})
    except Exception as exc:
        logger.exception("manage_project failed: %s", exc)
        return json.dumps({"error": str(exc)})


__all__ = [name for name in globals() if not name.startswith("__")]
