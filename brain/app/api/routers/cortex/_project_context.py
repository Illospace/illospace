"""Project Context profile and thought attachment endpoints."""
from __future__ import annotations

from typing import Any

from fastapi import Depends, File, Form, HTTPException, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from brain.app.api.auth import get_current_user
from brain.app.api.authorization import require_org_context
from brain.app.api.deps import get_db
from brain.app.api.db_utils import run_db
from brain.app.api.routers.cortex._helpers import UPLOAD_DIR, _caller_is_service_principal, _require_idea_for_user
from brain.app.api.routers.cortex._router import router
from brain.systems.cortex.project_context.github import (
    GitHubConnectorError,
    connect_with_token,
    get_repo_by_slug,
    parse_github_repo_slug,
    search_repos,
)
from brain.systems.cortex.project_context.permissions import derive_project_permission_scope
from brain.systems.cortex.project_context.profiles import attachment_to_read, profile_to_read
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
    ProjectProfileCreate,
    ProjectProfileRead,
    ProjectProfileUpdate,
    ProjectResourceUpdate,
    ProjectResourcesCreate,
    ProjectResourcesReorder,
)
from brain.systems.cortex.project_context.snapshot import snapshot_from_project_context
from brain.systems.cortex.project_context.uploads import (
    ProjectContextUploadError,
    save_project_context_uploads,
)
from brain.systems.cortex.project_context import vault as project_context_vault
from brain.platform.db.models.idea import IdeaProjectAttachment, ProjectProfile


async def _run_db(db: AsyncSession, fn, /, *args, **kwargs):
    def _sync(sync_db: Session):
        return fn(sync_db, *args, **kwargs)

    return await run_db(db, _sync)


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


def _profile_lookup_stmt(profile_id: str, org_id: str | None, *, include_inactive: bool = False):
    stmt = _profile_scope_stmt(org_id).where(ProjectProfile.id == profile_id)
    if not include_inactive:
        stmt = stmt.where(ProjectProfile.active.is_(True))
    return stmt


def _get_project_profile(
    db: Session,
    profile_id: str,
    org_id: str | None,
    *,
    include_inactive: bool = False,
) -> ProjectProfile:
    profile = db.scalar(_profile_lookup_stmt(profile_id, org_id, include_inactive=include_inactive))
    if profile is None:
        raise HTTPException(status_code=404, detail="Project profile not found")
    return profile


def _validate_project_context(project_context: dict[str, Any]) -> None:
    snapshot = snapshot_from_project_context(project_context)
    if snapshot.get("status") == "invalid":
        raise HTTPException(status_code=422, detail={"validation_errors": snapshot.get("validation_errors") or []})


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
    _validate_project_context(context)
    profile.project_context = context


@router.get("/project-context/profiles", response_model=list[ProjectProfileRead])
async def list_project_profiles(
    include_inactive: bool = False,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _list(sync_db: Session):
        org_id = _profile_org_id(user)
        stmt = _profile_scope_stmt(org_id)
        if not include_inactive:
            stmt = stmt.where(ProjectProfile.active.is_(True))
        stmt = stmt.order_by(ProjectProfile.created_at.desc())
        return [profile_to_read(profile) for profile in sync_db.scalars(stmt).all()]

    return await _run_db(db, _list)


@router.post("/project-context/profiles", response_model=ProjectProfileRead, status_code=201)
async def create_project_profile(
    body: ProjectProfileCreate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _create(sync_db: Session):
        org_id = _profile_org_id(user)
        _validate_project_context(body.project_context)
        existing_stmt = _profile_scope_stmt(org_id).where(ProjectProfile.slug == body.slug)
        existing = sync_db.scalar(existing_stmt)
        if existing is not None:
            raise HTTPException(status_code=409, detail="Project profile slug already exists")
        profile = ProjectProfile(
            org_id=org_id,
            user_id=str(user.get("id")) if user.get("id") else None,
            slug=body.slug,
            name=body.name,
            description=body.description,
            project_context=body.project_context,
            default_environment_binding_id=body.default_environment_binding_id,
            metadata_=body.metadata,
        )
        sync_db.add(profile)
        sync_db.commit()
        sync_db.refresh(profile)
        return profile_to_read(profile)

    return await _run_db(db, _create)


@router.get("/project-context/profiles/{profile_id}", response_model=ProjectProfileRead)
async def get_project_profile(
    profile_id: str,
    include_inactive: bool = False,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    return await _run_db(
        db,
        lambda sync_db: profile_to_read(
            _get_project_profile(sync_db, profile_id, _profile_org_id(user), include_inactive=include_inactive)
        ),
    )


@router.patch("/project-context/profiles/{profile_id}", response_model=ProjectProfileRead)
async def update_project_profile(
    profile_id: str,
    body: ProjectProfileUpdate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _update(sync_db: Session):
        org_id = _profile_org_id(user)
        profile = _get_project_profile(sync_db, profile_id, org_id, include_inactive=True)
        fields = body.model_fields_set
        if "slug" in fields and body.slug and body.slug != profile.slug:
            existing = sync_db.scalar(
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
            _validate_project_context(body.project_context)
            profile.project_context = body.project_context
        if "default_environment_binding_id" in fields:
            profile.default_environment_binding_id = body.default_environment_binding_id
        if "active" in fields and body.active is not None:
            profile.active = body.active
        if "metadata" in fields:
            profile.metadata_ = body.metadata or {}
        sync_db.add(profile)
        sync_db.commit()
        sync_db.refresh(profile)
        return profile_to_read(profile)

    return await _run_db(db, _update)


@router.delete("/project-context/profiles/{profile_id}", response_model=ProjectProfileRead)
async def archive_project_profile(
    profile_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _archive(sync_db: Session):
        org_id = _profile_org_id(user)
        profile = _get_project_profile(sync_db, profile_id, org_id, include_inactive=True)
        profile.active = False
        sync_db.add(profile)
        sync_db.commit()
        sync_db.refresh(profile)
        return profile_to_read(profile)

    return await _run_db(db, _archive)


@router.post("/project-context/profiles/{profile_id}/resources", response_model=ProjectProfileRead, status_code=201)
async def add_project_resources(
    profile_id: str,
    body: ProjectResourcesCreate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _add(sync_db: Session):
        org_id = _profile_org_id(user)
        profile = _get_project_profile(sync_db, profile_id, org_id, include_inactive=True)
        resources = _project_resources(profile)
        existing_ids = {str(resource.get("id")) for resource in resources if resource.get("id")}
        for raw in body.resources:
            resource = normalize_project_resource(raw, index=len(resources))
            resource["id"] = _unique_resource_id(resource, existing_ids, len(resources))
            resources.append(resource)
        _replace_project_resources(profile, resources)
        sync_db.add(profile)
        sync_db.commit()
        sync_db.refresh(profile)
        return profile_to_read(profile)

    return await _run_db(db, _add)


@router.patch("/project-context/profiles/{profile_id}/resources/{resource_id}", response_model=ProjectProfileRead)
async def update_project_resource(
    profile_id: str,
    resource_id: str,
    body: ProjectResourceUpdate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _update(sync_db: Session):
        org_id = _profile_org_id(user)
        profile = _get_project_profile(sync_db, profile_id, org_id, include_inactive=True)
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
        sync_db.add(profile)
        sync_db.commit()
        sync_db.refresh(profile)
        return profile_to_read(profile)

    return await _run_db(db, _update)


@router.delete("/project-context/profiles/{profile_id}/resources/{resource_id}", response_model=ProjectProfileRead)
async def remove_project_resource(
    profile_id: str,
    resource_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _remove(sync_db: Session):
        org_id = _profile_org_id(user)
        profile = _get_project_profile(sync_db, profile_id, org_id, include_inactive=True)
        resources = _project_resources(profile)
        next_resources = [
            resource
            for resource in resources
            if _resource_identity(resource) != resource_id and str(resource.get("id") or "") != resource_id
        ]
        if len(next_resources) == len(resources):
            raise HTTPException(status_code=404, detail="Project resource not found")
        _replace_project_resources(profile, next_resources)
        sync_db.add(profile)
        sync_db.commit()
        sync_db.refresh(profile)
        return profile_to_read(profile)

    return await _run_db(db, _remove)


@router.post("/project-context/profiles/{profile_id}/resources/reorder", response_model=ProjectProfileRead)
async def reorder_project_resources(
    profile_id: str,
    body: ProjectResourcesReorder,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _reorder(sync_db: Session):
        org_id = _profile_org_id(user)
        profile = _get_project_profile(sync_db, profile_id, org_id, include_inactive=True)
        resources = _project_resources(profile)
        by_id = {str(resource.get("id") or _resource_identity(resource)): resource for resource in resources}
        requested = [str(resource_id) for resource_id in body.resource_ids]
        if len(requested) != len(by_id) or len(set(requested)) != len(requested) or set(requested) != set(by_id):
            raise HTTPException(status_code=422, detail="resource_ids must include every project resource id exactly once")
        _replace_project_resources(profile, [by_id[resource_id] for resource_id in requested])
        sync_db.add(profile)
        sync_db.commit()
        sync_db.refresh(profile)
        return profile_to_read(profile)

    return await _run_db(db, _reorder)


@router.post("/project-context/github/connect", response_model=GitHubConnectRead)
def connect_github_project_context(
    body: GitHubVaultTokenRequest,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
):
    try:
        token = project_context_vault.github_token_from_vault(
            body.vault_key,
            user=user,
            unlock_token=request.headers.get(project_context_vault.VAULT_UNLOCK_HEADER),
        )
    except project_context_vault.ProjectContextVaultError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    try:
        return connect_with_token(token)
    except GitHubConnectorError as exc:
        raise _github_error_to_http(exc) from exc


@router.post("/project-context/github/search", response_model=GitHubRepoSearchRead)
def search_github_project_context(
    body: GitHubRepoSearchRequest,
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
):
    try:
        token = project_context_vault.github_token_from_vault(
            body.vault_key,
            user=user,
            unlock_token=request.headers.get(project_context_vault.VAULT_UNLOCK_HEADER),
        ) if body.vault_key else None
    except project_context_vault.ProjectContextVaultError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    try:
        return search_repos(body.query, token=token)
    except GitHubConnectorError as exc:
        raise _github_error_to_http(exc) from exc


@router.post("/project-context/github/bind-token", response_model=GitHubProjectTokenBindRead, status_code=201)
def bind_github_project_token(
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
        token = project_context_vault.github_token_from_vault(
            body.vault_key,
            user=user,
            unlock_token=request.headers.get(project_context_vault.VAULT_UNLOCK_HEADER),
            allow_shared=False,
        )
    except project_context_vault.ProjectContextVaultError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    try:
        repo = get_repo_by_slug(repo_slug, token=token)
    except GitHubConnectorError as exc:
        raise _github_error_to_http(exc) from exc
    if repo is None:
        raise HTTPException(status_code=404, detail="Repository not visible to this token")

    from brain.systems.vault import bind_project_secret_by_key

    try:
        binding = bind_project_secret_by_key(
            body.vault_key,
            user_id=user_id,
            org_id=str(user.get("org_id")) if user.get("org_id") else None,
            project_slug=repo_slug,
            env_name=body.env_name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if binding is None:
        raise HTTPException(status_code=404, detail="Project agent access requires a GitHub token you own")

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


@router.get("/ideas/{idea_id}/project-context", response_model=list[IdeaProjectAttachmentRead])
async def list_idea_project_context(
    idea_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _list(sync_db: Session):
        _require_idea_for_user(sync_db, idea_id, user)
        stmt = (
            select(IdeaProjectAttachment)
            .where(IdeaProjectAttachment.idea_id == idea_id)
            .order_by(IdeaProjectAttachment.created_at.desc(), IdeaProjectAttachment.id.desc())
        )
        return [attachment_to_read(attachment) for attachment in sync_db.scalars(stmt).all()]

    return await _run_db(db, _list)


@router.post("/ideas/{idea_id}/project-context", response_model=IdeaProjectAttachmentRead, status_code=201)
async def attach_idea_project_context(
    idea_id: str,
    body: IdeaProjectAttachmentCreate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _attach(sync_db: Session):
        idea = _require_idea_for_user(sync_db, idea_id, user)
        project_context = body.project_context
        profile: ProjectProfile | None = None
        if body.project_profile_id:
            org_id = _profile_org_id(user)
            profile = _get_project_profile(sync_db, body.project_profile_id, org_id)
            project_context = dict(profile.project_context or {})
        if not project_context:
            raise HTTPException(status_code=422, detail="project_profile_id or project_context is required")
        snapshot = snapshot_from_project_context(project_context)
        if snapshot.get("status") == "invalid":
            raise HTTPException(status_code=422, detail={"validation_errors": snapshot.get("validation_errors") or []})
        permission_scope = snapshot.get("permission_scope") or derive_project_permission_scope(snapshot)
        attachment = IdeaProjectAttachment(
            idea_id=idea_id,
            project_profile_id=profile.id if profile else body.project_profile_id,
            attached_by=str(user.get("id")) if user.get("id") else None,
            snapshot=snapshot,
            permission_scope=permission_scope,
            status=str(snapshot.get("status") or "validated"),
            validation_errors=snapshot.get("validation_errors") or [],
            environment_binding_id=body.environment_binding_id
            if body.environment_binding_id is not None
            else (profile.default_environment_binding_id if profile else None),
            metadata_=body.metadata,
        )
        _set_idea_project_context(idea, snapshot)
        sync_db.add(attachment)
        sync_db.commit()
        sync_db.refresh(attachment)
        return attachment_to_read(attachment)

    return await _run_db(db, _attach)
