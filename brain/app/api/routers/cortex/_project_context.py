"""Project Context profile and thought attachment endpoints."""
from __future__ import annotations

import json
from typing import Any

from fastapi import Depends, File, Form, HTTPException, Request, UploadFile
from starlette.responses import FileResponse
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.app.api.auth import get_current_user
from brain.app.api.authorization import require_org_context
from brain.app.api.deps import get_db
from brain.app.api.routers.cortex._helpers import UPLOAD_DIR, _caller_is_service_principal
from brain.app.api.routers.cortex._router import router
from brain.systems.cortex.project_context.github import (
    GitHubConnectorError,
    async_connect_with_token,
    async_get_repo_by_slug,
    async_search_repos,
    parse_github_repo_slug,
)
from brain.systems.cortex.project_context.browser import (
    project_file_blob,
    project_file_payload,
    update_project_draft_file,
    with_project_file_browser,
)
from brain.systems.cortex.project_context.identity import stamped_project_context
from brain.systems.cortex.project_context.profiles import attachment_to_read, profile_to_read
from brain.systems.cortex.project_context.access import (
    can_manage_project_profile,
    normalize_project_visibility,
    project_profile_visible_predicate,
)
from brain.systems.cortex.project_context.resources import normalize_project_resource
from brain.systems.cortex.project_context.schemas import (
    GitHubConnectRead,
    GitHubProjectTokenBindRead,
    GitHubProjectTokenBindRequest,
    GitHubRepoSearchRead,
    GitHubRepoSearchRequest,
    GitHubVaultTokenRequest,
    IdeaProjectAttachmentCreate,
    IdeaProjectAttachmentRead,
    ProjectDraftFileUpdate,
    ProjectProfileCreate,
    ProjectProfileRead,
    ProjectProfileUpdate,
    ProjectResourceUpdate,
    ProjectResourcesCreate,
    ProjectResourcesReorder,
)
from brain.systems.cortex.project_context.snapshot import (
    ProjectContextValidationError,
    validated_project_context_snapshot,
)
from brain.systems.cortex.project_context.uploads import (
    ProjectContextUploadError,
    save_project_context_uploads,
)
from brain.systems.cortex.project_context import vault as project_context_vault
from brain.systems.runs.execution_context import bind_agent_context
from brain.systems.runs.tool_catalog.handlers import projects as project_tools
from brain.platform.db.models.run import AgentRun
from brain.platform.db.models.idea import IdeaProjectAttachment, ProjectProfile, ProjectProfileAccess
from brain.platform.db.models.idea import Idea
from brain.platform.db.models.org import User


def _profile_org_id(user: dict[str, Any]) -> str | None:
    if _caller_is_service_principal(user):
        return user.get("org_id") or None
    return require_org_context(user)


def _github_error_to_http(exc: GitHubConnectorError) -> HTTPException:
    status_code = exc.status_code
    if status_code == 401:
        status_code = 400
    if status_code >= 500:
        status_code = 502
    return HTTPException(status_code=status_code, detail=exc.message)


def _set_idea_project_context(idea: Any, snapshot: dict[str, Any]) -> None:
    details = dict(idea.agent_details or {}) if isinstance(idea.agent_details, dict) else {}
    details["project_context"] = snapshot
    idea.agent_details = details


def _profile_scope_stmt(org_id: str | None):
    stmt = select(ProjectProfile)
    if org_id is None:
        return stmt.where(ProjectProfile.org_id.is_(None))
    return stmt.where(ProjectProfile.org_id == org_id)


def _actor_user_id(user: dict[str, Any] | None) -> str | None:
    user_id = str((user or {}).get("id") or "").strip()
    return user_id or None


def _profile_visible_stmt(org_id: str | None, user: dict[str, Any] | None):
    stmt = _profile_scope_stmt(org_id)
    if _caller_is_service_principal(user):
        return stmt
    return stmt.where(project_profile_visible_predicate(ProjectProfile, ProjectProfileAccess, _actor_user_id(user)))


def _profile_lookup_stmt(
    profile_id: str,
    org_id: str | None,
    user: dict[str, Any] | None,
    *,
    include_inactive: bool = False,
):
    stmt = _profile_scope_stmt(org_id).where(ProjectProfile.id == profile_id)
    if not _caller_is_service_principal(user):
        stmt = stmt.where(project_profile_visible_predicate(ProjectProfile, ProjectProfileAccess, _actor_user_id(user)))
    if not include_inactive:
        stmt = stmt.where(ProjectProfile.active.is_(True))
    return stmt


async def _get_project_profile(
    db: AsyncSession,
    profile_id: str,
    org_id: str | None,
    user: dict[str, Any] | None,
    *,
    include_inactive: bool = False,
) -> ProjectProfile:
    profile = await db.scalar(_profile_lookup_stmt(profile_id, org_id, user, include_inactive=include_inactive))
    if profile is None:
        raise HTTPException(status_code=404, detail="Project profile not found")
    return profile


def _require_project_profile_manager(profile: ProjectProfile, user: dict[str, Any] | None) -> None:
    if not can_manage_project_profile(profile, user):
        raise HTTPException(status_code=403, detail="Only the project owner can change this project")


def _request_visibility(value: str | None) -> str:
    try:
        return normalize_project_visibility(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _clean_shared_usernames(usernames: list[str] | None) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for username in usernames or []:
        value = str(username or "").strip()
        key = value.lower()
        if not value or key in seen:
            continue
        cleaned.append(value)
        seen.add(key)
    return cleaned


async def _resolve_project_access_users(
    db: AsyncSession,
    org_id: str | None,
    usernames: list[str] | None,
) -> list[User]:
    cleaned = _clean_shared_usernames(usernames)
    if not cleaned:
        return []
    if not org_id:
        raise HTTPException(status_code=422, detail="Project sharing requires an org-scoped user")

    lookup_keys = {item.lower() for item in cleaned}
    users = (
        await db.scalars(
            select(User).where(
                User.org_id == org_id,
                func.lower(User.name).in_(lookup_keys),
            )
        )
    ).all()
    users_by_key: dict[str, list[User]] = {}
    for user in users:
        key = str(user.name or "").strip().lower()
        if key and key in lookup_keys:
            users_by_key.setdefault(key, []).append(user)
    missing = [username for username in cleaned if username.lower() not in users_by_key]
    if missing:
        raise HTTPException(status_code=422, detail={"unknown_users": missing})
    ambiguous = [username for username in cleaned if len(users_by_key.get(username.lower(), [])) > 1]
    if ambiguous:
        raise HTTPException(status_code=422, detail={"ambiguous_users": ambiguous})
    ordered: list[User] = []
    seen_ids: set[str] = set()
    for username in cleaned:
        matches = users_by_key.get(username.lower()) or []
        if not matches:
            continue
        matched = matches[0]
        matched_id = str(matched.id)
        if matched_id in seen_ids:
            continue
        ordered.append(matched)
        seen_ids.add(matched_id)
    return ordered


async def _sync_project_access_list(
    db: AsyncSession,
    profile: ProjectProfile,
    *,
    org_id: str | None,
    shared_usernames: list[str] | None,
    actor_user_id: str | None,
) -> None:
    users = await _resolve_project_access_users(db, org_id, shared_usernames)
    owner_user_id = str(profile.user_id or "")
    target_user_ids = [
        str(user.id)
        for user in users
        if str(user.id) != owner_user_id
    ]
    target_user_ids = list(dict.fromkeys(target_user_ids))

    if target_user_ids:
        await db.execute(
            delete(ProjectProfileAccess).where(
                ProjectProfileAccess.project_profile_id == profile.id,
                ProjectProfileAccess.shared_with_user_id.not_in(target_user_ids),
            )
        )
    else:
        await db.execute(
            delete(ProjectProfileAccess).where(ProjectProfileAccess.project_profile_id == profile.id)
        )

    existing_ids = {
        str(row.shared_with_user_id)
        for row in (
            await db.scalars(
                select(ProjectProfileAccess).where(ProjectProfileAccess.project_profile_id == profile.id)
            )
        ).all()
    }
    for user_id in target_user_ids:
        if user_id in existing_ids:
            continue
        db.add(
            ProjectProfileAccess(
                project_profile_id=profile.id,
                shared_with_user_id=user_id,
                shared_by_user_id=actor_user_id,
            )
        )


async def _access_map_for_profiles(
    db: AsyncSession,
    profiles: list[ProjectProfile],
) -> dict[str, list[dict[str, Any]]]:
    profile_ids = [str(profile.id) for profile in profiles if profile.id]
    if not profile_ids:
        return {}
    rows = (
        await db.execute(
            select(ProjectProfileAccess, User)
            .join(User, User.id == ProjectProfileAccess.shared_with_user_id)
            .where(ProjectProfileAccess.project_profile_id.in_(profile_ids))
            .order_by(User.name.asc())
        )
    ).all()
    access_by_profile: dict[str, list[dict[str, Any]]] = {profile_id: [] for profile_id in profile_ids}
    for access, shared_user in rows:
        access_by_profile.setdefault(str(access.project_profile_id), []).append({
            "user_id": str(shared_user.id),
            "name": shared_user.name,
            "email": shared_user.email,
            "shared_by_user_id": str(access.shared_by_user_id) if access.shared_by_user_id else None,
            "created_at": access.created_at,
        })
    return access_by_profile


async def _require_idea_for_user(
    db: AsyncSession,
    idea_id: str,
    user: dict | None,
    *,
    detail: str = "Idea not found",
) -> Idea:
    if _caller_is_service_principal(user):
        idea = await db.get(Idea, idea_id)
    else:
        org_id = require_org_context(user or {})
        org_user_ids = select(User.id).where(User.org_id == str(org_id))
        idea = (
            await db.scalars(
                select(Idea).where(
                    Idea.id == idea_id,
                    (
                        (Idea.org_id == str(org_id))
                        | (Idea.org_id.is_(None) & Idea.user_id.in_(org_user_ids))
                    ),
                )
            )
        ).first()
    if idea is None:
        raise HTTPException(status_code=404, detail=detail)
    return idea


def _validated_snapshot_or_422(project_context: dict[str, Any]) -> dict[str, Any]:
    try:
        return validated_project_context_snapshot(project_context)
    except ProjectContextValidationError as exc:
        raise HTTPException(status_code=422, detail={"validation_errors": exc.errors}) from exc


def _profile_project_context(profile: ProjectProfile, project_context: dict[str, Any] | None = None) -> dict[str, Any]:
    return _validated_snapshot_or_422(stamped_project_context(profile, project_context))


def _resource_identity(resource: dict[str, Any]) -> str:
    value = resource.get("id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    for key in ("path", "uri", "repo", "name", "label"):
        value = resource.get(key)
        if isinstance(value, str) and value.strip():
            return f"{key}:{value.strip()}"
    return ""


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


def _project_resources(profile: ProjectProfile) -> list[dict[str, Any]]:
    context = profile.project_context if isinstance(profile.project_context, dict) else {}
    return [dict(item) for item in (context.get("resources") or []) if isinstance(item, dict)]


def _replace_project_resources(profile: ProjectProfile, resources: list[dict[str, Any]]) -> None:
    context = dict(profile.project_context or {})
    context["resources"] = resources
    profile.project_context = _profile_project_context(profile, context)


def _empty_project_change_summary() -> dict[str, Any]:
    paths = {
        "changed_paths": [],
        "new_paths": [],
        "deleted_paths": [],
        "conflicted_paths": [],
    }
    return {
        **paths,
        "counts": {key: 0 for key in paths},
        "total": 0,
    }


def _empty_project_draft_state_payload() -> dict[str, Any]:
    code = "project_run_not_found"
    error = "No AgentRun exists for this Cortex thread."
    return {
        "ok": False,
        "code": code,
        "error": error,
        "idea_id": None,
        "run_id": None,
        "draft_status": {
            "ok": False,
            "action": "draft_status",
            "code": code,
            "error": error,
            "idea_id": None,
            "run_id": None,
            "workspaces": [],
            "materialization": {},
            "resources": [],
            "changes": _empty_project_change_summary(),
        },
        "plan_publish": {
            "ok": False,
            "action": "plan_publish",
            "code": code,
            "error": error,
            "idea_id": None,
            "run_id": None,
            "mutates_project_root": False,
            "plan_only": True,
            "summary": {"resource_count": 0, "operation_count": 0, "blocked_count": 0},
            "groups": [],
        },
        "root_versions": {
            "ok": False,
            "action": "root_versions",
            "code": code,
            "error": error,
            "idea_id": None,
            "run_id": None,
            "summary": {"resource_count": 0, "version_count": 0},
            "groups": [],
        },
    }


async def _manage_project_payload(action: str) -> dict[str, Any]:
    return json.loads(await project_tools._handle_manage_project(action=action))


async def _project_draft_state_payload(
    run: AgentRun | None,
    *,
    idea_id: str,
    user: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if run is None:
        return _empty_project_draft_state_payload()

    run_id = str(getattr(run, "id", "") or "")
    org_id = str(getattr(run, "org_id", "") or (user or {}).get("org_id") or "") or None
    user_id = str(getattr(run, "user_id", "") or (user or {}).get("id") or "") or None
    context = {
        "run": run,
        "run_id": run_id,
        "idea_id": str(idea_id),
        "org_id": org_id,
        "user_id": user_id,
        "execution_metadata": {
            "run_id": run_id,
            "idea_id": str(idea_id),
            "org_id": org_id,
            "user_id": user_id,
        },
    }
    with bind_agent_context(context):
        draft_status = await _manage_project_payload("draft_status")
        plan_publish = await _manage_project_payload("plan_publish")
        root_versions = await _manage_project_payload("root_versions")
    draft_status = with_project_file_browser(draft_status)

    return {
        "ok": bool(
            draft_status.get("ok")
            and plan_publish.get("ok")
            and root_versions.get("ok")
        ),
        "idea_id": draft_status.get("idea_id") or str(idea_id),
        "run_id": draft_status.get("run_id") or run_id,
        "draft_status": draft_status,
        "plan_publish": plan_publish,
        "root_versions": root_versions,
    }


async def _project_draft_file_payload(
    run: AgentRun | None,
    *,
    idea_id: str,
    path: str,
    resource_id: str | None = None,
    user: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if run is None:
        raise HTTPException(status_code=404, detail="No AgentRun exists for this Cortex thread.")

    run_id = str(getattr(run, "id", "") or "")
    org_id = str(getattr(run, "org_id", "") or (user or {}).get("org_id") or "") or None
    user_id = str(getattr(run, "user_id", "") or (user or {}).get("id") or "") or None
    context = {
        "run": run,
        "run_id": run_id,
        "idea_id": str(idea_id),
        "org_id": org_id,
        "user_id": user_id,
        "execution_metadata": {
            "run_id": run_id,
            "idea_id": str(idea_id),
            "org_id": org_id,
            "user_id": user_id,
        },
    }
    with bind_agent_context(context):
        draft_status = await _manage_project_payload("draft_status")
    if not draft_status.get("ok"):
        raise HTTPException(status_code=404, detail=draft_status.get("error") or "Project draft state is unavailable.")
    try:
        return project_file_payload(draft_status, resource_id=resource_id, path=path)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


async def _project_draft_file_blob_response(
    run: AgentRun | None,
    *,
    idea_id: str,
    path: str,
    layer: str,
    resource_id: str | None = None,
    user: dict[str, Any] | None = None,
) -> FileResponse:
    if run is None:
        raise HTTPException(status_code=404, detail="No AgentRun exists for this Cortex thread.")

    run_id = str(getattr(run, "id", "") or "")
    org_id = str(getattr(run, "org_id", "") or (user or {}).get("org_id") or "") or None
    user_id = str(getattr(run, "user_id", "") or (user or {}).get("id") or "") or None
    context = {
        "run": run,
        "run_id": run_id,
        "idea_id": str(idea_id),
        "org_id": org_id,
        "user_id": user_id,
        "execution_metadata": {
            "run_id": run_id,
            "idea_id": str(idea_id),
            "org_id": org_id,
            "user_id": user_id,
        },
    }
    with bind_agent_context(context):
        draft_status = await _manage_project_payload("draft_status")
    if not draft_status.get("ok"):
        raise HTTPException(status_code=404, detail=draft_status.get("error") or "Project draft state is unavailable.")
    try:
        blob = project_file_blob(draft_status, resource_id=resource_id, path=path, layer=layer)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(
        blob["path"],
        media_type=blob["media_type"],
        filename=blob["filename"],
        content_disposition_type="inline",
    )


async def _update_project_draft_file_payload(
    run: AgentRun | None,
    *,
    idea_id: str,
    body: ProjectDraftFileUpdate,
    user: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if run is None:
        raise HTTPException(status_code=404, detail="No AgentRun exists for this Cortex thread.")

    run_id = str(getattr(run, "id", "") or "")
    org_id = str(getattr(run, "org_id", "") or (user or {}).get("org_id") or "") or None
    user_id = str(getattr(run, "user_id", "") or (user or {}).get("id") or "") or None
    context = {
        "run": run,
        "run_id": run_id,
        "idea_id": str(idea_id),
        "org_id": org_id,
        "user_id": user_id,
        "execution_metadata": {
            "run_id": run_id,
            "idea_id": str(idea_id),
            "org_id": org_id,
            "user_id": user_id,
        },
    }
    with bind_agent_context(context):
        draft_status = await _manage_project_payload("draft_status")
    if not draft_status.get("ok"):
        raise HTTPException(status_code=404, detail=draft_status.get("error") or "Project draft state is unavailable.")
    try:
        return update_project_draft_file(
            draft_status,
            resource_id=body.resource_id,
            path=body.path,
            content=body.content,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _project_draft_state_run(
    db: AsyncSession,
    idea_id: str,
    run_id: int | None,
    user: dict[str, Any],
) -> AgentRun | None:
    await _require_idea_for_user(db, idea_id, user)
    if run_id is not None:
        run = await db.get(AgentRun, int(run_id))
        if run is None or str(getattr(run, "thread_id", "") or "") != str(idea_id):
            raise HTTPException(status_code=404, detail=f"Run #{run_id} not found for idea")
        return run

    stmt = (
        select(AgentRun)
        .where(AgentRun.thread_id == str(idea_id))
        .order_by(AgentRun.created_at.desc(), AgentRun.id.desc())
        .limit(1)
    )
    return (await db.scalars(stmt)).first()


@router.get("/project-context/profiles", response_model=list[ProjectProfileRead])
async def list_project_profiles(
    include_inactive: bool = False,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    org_id = _profile_org_id(user)
    stmt = _profile_visible_stmt(org_id, user)
    if not include_inactive:
        stmt = stmt.where(ProjectProfile.active.is_(True))
    stmt = stmt.order_by(ProjectProfile.created_at.desc())
    profiles = (await db.scalars(stmt)).all()
    access_by_profile = await _access_map_for_profiles(db, list(profiles))
    return [
        profile_to_read(profile, access_by_profile.get(str(profile.id), []))
        for profile in profiles
    ]


@router.post("/project-context/profiles", response_model=ProjectProfileRead, status_code=201)
async def create_project_profile(
    body: ProjectProfileCreate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    org_id = _profile_org_id(user)
    visibility = _request_visibility(body.visibility)
    project_context = _validated_snapshot_or_422(body.project_context)
    existing_stmt = _profile_scope_stmt(org_id).where(ProjectProfile.slug == body.slug)
    existing = await db.scalar(existing_stmt)
    if existing is not None:
        raise HTTPException(status_code=409, detail="Project profile slug already exists")
    profile = ProjectProfile(
        org_id=org_id,
        user_id=str(user.get("id")) if user.get("id") else None,
        slug=body.slug,
        name=body.name,
        description=body.description,
        project_context=project_context,
        visibility=visibility,
        default_environment_binding_id=body.default_environment_binding_id,
        metadata_=body.metadata,
    )
    db.add(profile)
    await db.flush()
    profile.project_context = _profile_project_context(profile, project_context)
    await _sync_project_access_list(
        db,
        profile,
        org_id=org_id,
        shared_usernames=body.shared_usernames,
        actor_user_id=_actor_user_id(user),
    )
    await db.commit()
    await db.refresh(profile)
    access_by_profile = await _access_map_for_profiles(db, [profile])
    return profile_to_read(profile, access_by_profile.get(str(profile.id), []))


@router.get("/project-context/profiles/{profile_id}", response_model=ProjectProfileRead)
async def get_project_profile(
    profile_id: str,
    include_inactive: bool = False,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    profile = await _get_project_profile(db, profile_id, _profile_org_id(user), user, include_inactive=include_inactive)
    access_by_profile = await _access_map_for_profiles(db, [profile])
    return profile_to_read(profile, access_by_profile.get(str(profile.id), []))


@router.patch("/project-context/profiles/{profile_id}", response_model=ProjectProfileRead)
async def update_project_profile(
    profile_id: str,
    body: ProjectProfileUpdate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    org_id = _profile_org_id(user)
    profile = await _get_project_profile(db, profile_id, org_id, user, include_inactive=True)
    _require_project_profile_manager(profile, user)
    fields = body.model_fields_set
    if "slug" in fields and body.slug and body.slug != profile.slug:
        existing = await db.scalar(
            _profile_scope_stmt(org_id).where(
                ProjectProfile.slug == body.slug,
                ProjectProfile.id != profile.id,
            )
        )
        if existing is not None:
            raise HTTPException(status_code=409, detail="Project profile slug already exists")
        profile.slug = body.slug
    if "name" in fields and body.name is not None:
        profile.name = body.name
    if "description" in fields:
        profile.description = body.description
    if "project_context" in fields and body.project_context is not None:
        profile.project_context = _profile_project_context(profile, _validated_snapshot_or_422(body.project_context))
    if "visibility" in fields and body.visibility is not None:
        profile.visibility = _request_visibility(body.visibility)
    if "default_environment_binding_id" in fields:
        profile.default_environment_binding_id = body.default_environment_binding_id
    if "active" in fields and body.active is not None:
        profile.active = body.active
    if "metadata" in fields:
        profile.metadata_ = body.metadata or {}
    profile.project_context = _profile_project_context(profile)
    db.add(profile)
    if "shared_usernames" in fields and body.shared_usernames is not None:
        await _sync_project_access_list(
            db,
            profile,
            org_id=org_id,
            shared_usernames=body.shared_usernames,
            actor_user_id=_actor_user_id(user),
        )
    await db.commit()
    await db.refresh(profile)
    access_by_profile = await _access_map_for_profiles(db, [profile])
    return profile_to_read(profile, access_by_profile.get(str(profile.id), []))


@router.delete("/project-context/profiles/{profile_id}", response_model=ProjectProfileRead)
async def archive_project_profile(
    profile_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    org_id = _profile_org_id(user)
    profile = await _get_project_profile(db, profile_id, org_id, user, include_inactive=True)
    _require_project_profile_manager(profile, user)
    profile.active = False
    db.add(profile)
    await db.commit()
    await db.refresh(profile)
    access_by_profile = await _access_map_for_profiles(db, [profile])
    return profile_to_read(profile, access_by_profile.get(str(profile.id), []))


@router.post("/project-context/profiles/{profile_id}/resources", response_model=ProjectProfileRead, status_code=201)
async def add_project_resources(
    profile_id: str,
    body: ProjectResourcesCreate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    org_id = _profile_org_id(user)
    profile = await _get_project_profile(db, profile_id, org_id, user, include_inactive=True)
    _require_project_profile_manager(profile, user)
    resources = _project_resources(profile)
    existing_ids = {str(resource.get("id")) for resource in resources if resource.get("id")}
    for raw in body.resources:
        resource = normalize_project_resource(raw, index=len(resources))
        resource["id"] = _unique_resource_id(resource, existing_ids, len(resources))
        resources.append(resource)
    _replace_project_resources(profile, resources)
    db.add(profile)
    await db.commit()
    await db.refresh(profile)
    access_by_profile = await _access_map_for_profiles(db, [profile])
    return profile_to_read(profile, access_by_profile.get(str(profile.id), []))


@router.patch("/project-context/profiles/{profile_id}/resources/{resource_id}", response_model=ProjectProfileRead)
async def update_project_resource(
    profile_id: str,
    resource_id: str,
    body: ProjectResourceUpdate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    org_id = _profile_org_id(user)
    profile = await _get_project_profile(db, profile_id, org_id, user, include_inactive=True)
    _require_project_profile_manager(profile, user)
    resources = _project_resources(profile)
    for index, resource in enumerate(resources):
        if _resource_identity(resource) != resource_id and str(resource.get("id") or "") != resource_id:
            continue
        raw = dict(body.resource or {}) if body.replace else {**resource, **dict(body.resource or {})}
        raw.setdefault("id", resource.get("id") or resource_id)
        resources[index] = normalize_project_resource(raw, index=index)
        break
    else:
        raise HTTPException(status_code=404, detail="Project resource not found")
    _replace_project_resources(profile, resources)
    db.add(profile)
    await db.commit()
    await db.refresh(profile)
    access_by_profile = await _access_map_for_profiles(db, [profile])
    return profile_to_read(profile, access_by_profile.get(str(profile.id), []))


@router.delete("/project-context/profiles/{profile_id}/resources/{resource_id}", response_model=ProjectProfileRead)
async def remove_project_resource(
    profile_id: str,
    resource_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    org_id = _profile_org_id(user)
    profile = await _get_project_profile(db, profile_id, org_id, user, include_inactive=True)
    _require_project_profile_manager(profile, user)
    resources = _project_resources(profile)
    next_resources = [
        resource
        for resource in resources
        if _resource_identity(resource) != resource_id and str(resource.get("id") or "") != resource_id
    ]
    if len(next_resources) == len(resources):
        raise HTTPException(status_code=404, detail="Project resource not found")
    _replace_project_resources(profile, next_resources)
    db.add(profile)
    await db.commit()
    await db.refresh(profile)
    access_by_profile = await _access_map_for_profiles(db, [profile])
    return profile_to_read(profile, access_by_profile.get(str(profile.id), []))


@router.post("/project-context/profiles/{profile_id}/resources/reorder", response_model=ProjectProfileRead)
async def reorder_project_resources(
    profile_id: str,
    body: ProjectResourcesReorder,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    org_id = _profile_org_id(user)
    profile = await _get_project_profile(db, profile_id, org_id, user, include_inactive=True)
    _require_project_profile_manager(profile, user)
    resources = _project_resources(profile)
    by_id = {str(resource.get("id") or _resource_identity(resource)): resource for resource in resources}
    requested = [str(resource_id) for resource_id in body.resource_ids]
    if len(requested) != len(by_id) or len(set(requested)) != len(requested) or set(requested) != set(by_id):
        raise HTTPException(status_code=422, detail="resource_ids must include every project resource id exactly once")
    _replace_project_resources(profile, [by_id[resource_id] for resource_id in requested])
    db.add(profile)
    await db.commit()
    await db.refresh(profile)
    access_by_profile = await _access_map_for_profiles(db, [profile])
    return profile_to_read(profile, access_by_profile.get(str(profile.id), []))


@router.post("/project-context/github/connect", response_model=GitHubConnectRead)
async def connect_github_project_context(
    body: GitHubVaultTokenRequest,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
):
    try:
        token = await project_context_vault.async_github_token_from_vault(
            body.vault_key,
            user=user,
            unlock_token=request.headers.get(project_context_vault.VAULT_UNLOCK_HEADER),
        )
    except project_context_vault.ProjectContextVaultError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    try:
        return await async_connect_with_token(token)
    except GitHubConnectorError as exc:
        raise _github_error_to_http(exc) from exc


@router.post("/project-context/github/search", response_model=GitHubRepoSearchRead)
async def search_github_project_context(
    body: GitHubRepoSearchRequest,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
):
    try:
        token = await project_context_vault.async_github_token_from_vault(
            body.vault_key,
            user=user,
            unlock_token=request.headers.get(project_context_vault.VAULT_UNLOCK_HEADER),
        ) if body.vault_key else None
    except project_context_vault.ProjectContextVaultError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    try:
        return await async_search_repos(body.query, token=token)
    except GitHubConnectorError as exc:
        raise _github_error_to_http(exc) from exc


@router.post("/project-context/github/bind-token", response_model=GitHubProjectTokenBindRead, status_code=201)
async def bind_github_project_token(
    body: GitHubProjectTokenBindRequest,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
):
    user_id = str(user.get("id") or "")
    if not user_id or user_id.startswith("service:"):
        raise HTTPException(status_code=403, detail="Project GitHub tokens require a human user")
    repo_slug = parse_github_repo_slug(body.repo)
    if not repo_slug:
        raise HTTPException(status_code=400, detail="GitHub repository must be owner/repo or a GitHub URL")

    try:
        token = await project_context_vault.async_github_token_from_vault(
            body.vault_key,
            user=user,
            unlock_token=request.headers.get(project_context_vault.VAULT_UNLOCK_HEADER),
        )
    except project_context_vault.ProjectContextVaultError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    try:
        repo = await async_get_repo_by_slug(repo_slug, token=token)
    except GitHubConnectorError as exc:
        raise _github_error_to_http(exc) from exc
    if repo is None:
        raise HTTPException(status_code=404, detail="Repository not visible to this token")

    from brain.systems.vault import async_bind_project_secret_by_key

    try:
        binding = await async_bind_project_secret_by_key(
            body.vault_key,
            actor_user_id=user_id,
            org_id=str(user.get("org_id")) if user.get("org_id") else None,
            project_slug=repo_slug,
            env_name=body.env_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if binding is None:
        raise HTTPException(status_code=404, detail="Project agent access requires an org GitHub token")

    permissions = repo.get("permissions") if isinstance(repo.get("permissions"), dict) else {}
    return {
        "project_slug": repo_slug.lower(),
        "env_name": binding["env_name"],
        "binding": binding,
        "repo": repo,
        "write_access": bool(
            permissions.get("admin")
            or permissions.get("maintain")
            or permissions.get("push")
        ),
    }


@router.post("/project-context/local-files")
async def upload_project_context_local_files(
    files: list[UploadFile] = File(...),
    relative_paths: list[str] = Form(default=[]),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Upload browser-picked files so Project Context points at backend-readable paths."""

    try:
        return await save_project_context_uploads(files, relative_paths, upload_dir=UPLOAD_DIR)
    except ProjectContextUploadError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.get("/ideas/{idea_id}/project-context/draft-state")
async def get_idea_project_context_draft_state(
    idea_id: str,
    run_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    run = await _project_draft_state_run(db, idea_id, run_id, user)
    return await _project_draft_state_payload(run, idea_id=idea_id, user=user)


@router.get("/ideas/{idea_id}/project-context/draft-file")
async def get_idea_project_context_draft_file(
    idea_id: str,
    path: str,
    run_id: int | None = None,
    resource_id: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    run = await _project_draft_state_run(db, idea_id, run_id, user)
    return await _project_draft_file_payload(
        run,
        idea_id=idea_id,
        path=path,
        resource_id=resource_id,
        user=user,
    )


@router.get("/ideas/{idea_id}/project-context/draft-file/blob")
async def get_idea_project_context_draft_file_blob(
    idea_id: str,
    path: str,
    layer: str = "draft",
    run_id: int | None = None,
    resource_id: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    run = await _project_draft_state_run(db, idea_id, run_id, user)
    return await _project_draft_file_blob_response(
        run,
        idea_id=idea_id,
        path=path,
        layer=layer,
        resource_id=resource_id,
        user=user,
    )


@router.patch("/ideas/{idea_id}/project-context/draft-file")
async def update_idea_project_context_draft_file(
    idea_id: str,
    body: ProjectDraftFileUpdate,
    run_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    run = await _project_draft_state_run(db, idea_id, run_id, user)
    return await _update_project_draft_file_payload(
        run,
        idea_id=idea_id,
        body=body,
        user=user,
    )


@router.get("/ideas/{idea_id}/project-context", response_model=list[IdeaProjectAttachmentRead])
async def list_idea_project_context(
    idea_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    await _require_idea_for_user(db, idea_id, user)
    stmt = (
        select(IdeaProjectAttachment)
        .where(IdeaProjectAttachment.idea_id == idea_id)
        .order_by(IdeaProjectAttachment.created_at.desc(), IdeaProjectAttachment.id.desc())
    )
    return [attachment_to_read(attachment) for attachment in (await db.scalars(stmt)).all()]


@router.post("/ideas/{idea_id}/project-context", response_model=IdeaProjectAttachmentRead, status_code=201)
async def attach_idea_project_context(
    idea_id: str,
    body: IdeaProjectAttachmentCreate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    idea = await _require_idea_for_user(db, idea_id, user)
    project_context = body.project_context
    profile: ProjectProfile | None = None
    if body.project_profile_id:
        org_id = _profile_org_id(user)
        profile = await _get_project_profile(db, body.project_profile_id, org_id, user)
        project_context = _profile_project_context(profile)
    if not project_context:
        raise HTTPException(status_code=422, detail="project_profile_id or project_context is required")
    snapshot = _validated_snapshot_or_422(project_context)
    attachment = IdeaProjectAttachment(
        idea_id=idea_id,
        project_profile_id=profile.id if profile else body.project_profile_id,
        attached_by=str(user.get("id")) if user.get("id") else None,
        snapshot=snapshot,
        permission_scope=snapshot.get("permission_scope") or {},
        status=str(snapshot.get("status") or "validated"),
        validation_errors=snapshot.get("validation_errors") or [],
        environment_binding_id=body.environment_binding_id
        if body.environment_binding_id is not None
        else (profile.default_environment_binding_id if profile else None),
        metadata_=body.metadata,
    )
    _set_idea_project_context(idea, snapshot)
    db.add(attachment)
    await db.commit()
    await db.refresh(attachment)
    return attachment_to_read(attachment)
