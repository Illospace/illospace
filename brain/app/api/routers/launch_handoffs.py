"""Launch handoff API and redirect routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import RedirectResponse

from brain.app.api.auth import get_current_user
from brain.app.api.authorization import require_org_context
from brain.app.api.deps import get_db, rate_limit
from brain.app.api.schemas.launch_handoffs import LaunchHandoffCreateRequest
from brain.systems import launch_handoffs


router = APIRouter(tags=["launch-handoffs"], dependencies=[Depends(rate_limit)])


def _user_id(user: dict | None) -> str | None:
    return str(user.get("id")) if user and user.get("id") else None


async def _require_handoff_for_api(
    db: AsyncSession,
    handoff_id: str,
    *,
    org_id: str,
) -> launch_handoffs.LaunchHandoff:
    try:
        return await launch_handoffs.require_launch_handoff(db, handoff_id, org_id=org_id)
    except launch_handoffs.LaunchHandoffNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/launch-handoffs", status_code=201)
async def create_launch_handoff(
    payload: LaunchHandoffCreateRequest,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(get_current_user),
) -> dict:
    org_id = require_org_context(user)
    try:
        row = await launch_handoffs.create_launch_handoff(
            db,
            launch_handoffs.LaunchHandoffCreateInput(
                org_id=org_id,
                created_by_user_id=_user_id(user),
                title=payload.title,
                instructions=payload.instructions,
                target_tool=payload.target_tool,
                summary=payload.summary,
                source_surface=payload.source_surface,
                source_ref=payload.source_ref,
                context_parts=payload.context_parts,
                acceptance_criteria=payload.acceptance_criteria,
                repo_origin_url=payload.repo_origin_url,
                branch_hint=payload.branch_hint,
                idempotency_key=payload.idempotency_key,
                metadata=payload.metadata,
            ),
        )
    except launch_handoffs.LaunchHandoffError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"handoff": launch_handoffs.serialize_launch_handoff(row)}


@router.get("/api/launch-handoffs/{handoff_id}")
async def get_launch_handoff(
    handoff_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(get_current_user),
) -> dict:
    org_id = require_org_context(user)
    row = await _require_handoff_for_api(db, handoff_id, org_id=org_id)
    return {"handoff": launch_handoffs.serialize_launch_handoff(row)}


async def _redirect_to_handoff_target(
    handoff_id: str,
    *,
    target: str | None,
    db: AsyncSession,
    user: dict,
) -> RedirectResponse:
    org_id = require_org_context(user)
    try:
        row = await launch_handoffs.require_launch_handoff(db, handoff_id, org_id=org_id)
    except launch_handoffs.LaunchHandoffNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    target_tool = str(target or row.target_tool or launch_handoffs.TARGET_CODEX).strip().lower()
    if target_tool != launch_handoffs.TARGET_CODEX:
        raise HTTPException(status_code=400, detail=f"Unsupported launch target: {target_tool}")

    await launch_handoffs.mark_launch_handoff_launched(db, row, launched_by_user_id=_user_id(user))
    return RedirectResponse(launch_handoffs.codex_deep_link_for_handoff(row), status_code=302)


@router.get("/api/launch-handoffs/{handoff_id}/launch")
async def redirect_api_launch_handoff(
    handoff_id: str,
    target: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(get_current_user),
) -> RedirectResponse:
    return await _redirect_to_handoff_target(handoff_id, target=target, db=db, user=user)
