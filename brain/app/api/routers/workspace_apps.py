"""Generated workspace apps router."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from brain.app.api.auth import get_current_user
from brain.app.api.authorization import require_org_context
from brain.app.api.deps import get_db, rate_limit
from brain.app.api.schemas.workspace_apps import (
    WorkspaceAppActionRun,
    WorkspaceAppActionRunRead,
    WorkspaceAppCreate,
    WorkspaceAppRead,
    WorkspaceAppStateRead,
    WorkspaceAppStateUpdate,
    WorkspaceAppUpdate,
)
from brain.systems.workspace_apps.actions import (
    WorkspaceAppActionContractError,
    WorkspaceAppActionError,
    WorkspaceAppActionExecutorMissing,
    WorkspaceAppActionNotDeclared,
    async_run_workspace_app_action,
)
from brain.systems.workspace_apps.service import (
    WorkspaceAppConflict,
    WorkspaceAppContractError,
    WorkspaceAppError,
    WorkspaceAppNotFound,
    a_archive_app,
    a_create_app,
    a_delete_archived_apps,
    a_get_app,
    a_get_state,
    a_list_archived_apps,
    a_list_apps,
    a_restore_app,
    a_serialize_app,
    a_serialize_apps,
    a_update_app,
    a_update_state,
    serialize_state,
)
from brain.systems.workspace_apps.events import publish_workspace_app_change

router = APIRouter(
    prefix="/api/workspace-apps",
    tags=["workspace-apps"],
    dependencies=[Depends(rate_limit)],
)


def _user_id(user: dict[str, Any]) -> str | None:
    return str(user.get("id")) if user.get("id") else None


def _raise_http(exc: WorkspaceAppError) -> None:
    if isinstance(exc, WorkspaceAppNotFound):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, WorkspaceAppConflict):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, WorkspaceAppContractError):
        raise HTTPException(
            status_code=400,
            detail={"error": str(exc), "contract_validation": exc.report},
        ) from exc
    raise HTTPException(status_code=400, detail=str(exc)) from exc


def _raise_action_http(exc: WorkspaceAppActionError) -> None:
    if isinstance(exc, WorkspaceAppActionNotDeclared):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, WorkspaceAppActionExecutorMissing):
        raise HTTPException(status_code=501, detail=str(exc)) from exc
    if isinstance(exc, WorkspaceAppActionContractError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("", response_model=list[WorkspaceAppRead], include_in_schema=False)
@router.get("/", response_model=list[WorkspaceAppRead])
async def list_workspace_apps(
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    org_id = require_org_context(user)
    return await a_serialize_apps(db, await a_list_apps(db, org_id))


@router.get("/archived", response_model=list[WorkspaceAppRead])
async def list_archived_workspace_apps(
    limit: int = 12,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    org_id = require_org_context(user)
    return await a_serialize_apps(db, await a_list_archived_apps(db, org_id, limit=limit))


@router.delete("/archived")
async def empty_archived_workspace_apps(
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    org_id = require_org_context(user)
    deleted = await a_delete_archived_apps(db, org_id=org_id)
    await db.commit()
    if deleted:
        publish_workspace_app_change(org_id=org_id, action="empty_archive")
    return {"deleted": deleted}


@router.post("", response_model=WorkspaceAppRead, status_code=201, include_in_schema=False)
@router.post("/", response_model=WorkspaceAppRead, status_code=201)
async def create_workspace_app(
    body: WorkspaceAppCreate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    org_id = require_org_context(user)
    try:
        app = await a_create_app(
            db,
            org_id=org_id,
            key=body.key,
            name=body.name,
            description=body.description,
            renderer_key=body.renderer_key,
            source_kind=body.source_kind,
            source_code=body.source_code,
            manifest=body.manifest,
            visual_spec=body.visual_spec,
            metadata=body.metadata,
            created_by_user_id=_user_id(user),
            anchor_user_id=body.anchor_user_id,
            initial_state=body.initial_state,
            state_key=body.state_key,
        )
        serialized = await a_serialize_app(db, app)
        await db.commit()
        publish_workspace_app_change(org_id=org_id, action="create", app=serialized)
        return serialized
    except WorkspaceAppError as exc:
        _raise_http(exc)


@router.get("/{app_id}", response_model=WorkspaceAppRead)
async def get_workspace_app(
    app_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    org_id = require_org_context(user)
    try:
        return await a_serialize_app(db, await a_get_app(db, org_id, app_id))
    except WorkspaceAppError as exc:
        _raise_http(exc)


@router.patch("/{app_id}", response_model=WorkspaceAppRead)
async def update_workspace_app(
    app_id: str,
    body: WorkspaceAppUpdate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    org_id = require_org_context(user)
    try:
        app = await a_update_app(
            db,
            org_id=org_id,
            app_id=app_id,
            name=body.name,
            description=body.description,
            renderer_key=body.renderer_key,
            source_kind=body.source_kind,
            source_code=body.source_code,
            manifest=body.manifest,
            visual_spec=body.visual_spec,
            metadata=body.metadata,
            anchor_user_id=body.anchor_user_id,
            updated_by_user_id=_user_id(user),
        )
        serialized = await a_serialize_app(db, app)
        await db.commit()
        publish_workspace_app_change(org_id=org_id, action="update", app=serialized)
        return serialized
    except WorkspaceAppError as exc:
        _raise_http(exc)


@router.delete("/{app_id}")
async def archive_workspace_app(
    app_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    org_id = require_org_context(user)
    try:
        result = await a_archive_app(db, org_id=org_id, app_id=app_id)
        await db.commit()
        archived = result.get("archived", {})
        publish_workspace_app_change(
            org_id=org_id,
            action="archive",
            app_id=archived.get("id") or app_id,
            key=archived.get("key"),
        )
        return result
    except WorkspaceAppError as exc:
        _raise_http(exc)


@router.post("/{app_id}/restore", response_model=WorkspaceAppRead)
async def restore_workspace_app(
    app_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    org_id = require_org_context(user)
    try:
        app = await a_restore_app(db, org_id=org_id, app_id=app_id)
        serialized = await a_serialize_app(db, app)
        await db.commit()
        publish_workspace_app_change(org_id=org_id, action="restore", app=serialized)
        return serialized
    except WorkspaceAppError as exc:
        _raise_http(exc)


@router.get("/{app_id}/state/{state_key}", response_model=WorkspaceAppStateRead)
async def get_workspace_app_state(
    app_id: str,
    state_key: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    org_id = require_org_context(user)
    try:
        state = await a_get_state(db, org_id=org_id, app_id=app_id, key=state_key, user_id=_user_id(user))
        return serialize_state(state)
    except WorkspaceAppError as exc:
        _raise_http(exc)


@router.put("/{app_id}/state/{state_key}", response_model=WorkspaceAppStateRead)
async def update_workspace_app_state(
    app_id: str,
    state_key: str,
    body: WorkspaceAppStateUpdate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    org_id = require_org_context(user)
    try:
        state = await a_update_state(
            db,
            org_id=org_id,
            app_id=app_id,
            key=state_key,
            data=body.data,
            data_patch=body.data_patch,
            user_id=_user_id(user),
        )
        return serialize_state(state)
    except WorkspaceAppError as exc:
        _raise_http(exc)


@router.post("/{app_id}/actions/run", response_model=WorkspaceAppActionRunRead)
async def run_workspace_app_declared_action(
    app_id: str,
    body: WorkspaceAppActionRun,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    org_id = require_org_context(user)
    try:
        result = await async_run_workspace_app_action(
            db,
            org_id=org_id,
            app_id=app_id,
            action_key=body.action_key,
            payload=body.payload,
            user_id=_user_id(user),
        )
        await db.commit()
        return result
    except WorkspaceAppActionError as exc:
        _raise_action_http(exc)
    except WorkspaceAppError as exc:
        _raise_http(exc)
