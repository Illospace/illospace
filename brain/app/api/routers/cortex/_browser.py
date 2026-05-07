"""Cortex browser session endpoints."""
from __future__ import annotations

from typing import Any

from fastapi import Depends, HTTPException

from brain.app.api.auth import get_current_user
from brain.app.api.routers.cortex._helpers import _validate_idea_org_orm
from brain.app.api.routers.cortex._router import router
from brain.app.api.schemas.ideas import BrowserSessionCreate, BrowserSessionRead
from brain.platform.browser import BrowserCapabilityError, browser_sessions
from brain.platform.db.models.browser import BrowserSession
from brain.platform.db.repositories.unit_of_work import UnitOfWork


def _get_browser_session_or_404(session_id: str) -> BrowserSession:
    with UnitOfWork() as uow:
        record = uow.session.get(BrowserSession, session_id)
        if not record:
            raise HTTPException(status_code=404, detail="Browser session not found")
        return record


def _get_browser_session_for_user_or_404(session_id: str, user: dict[str, Any]) -> BrowserSession:
    record = browser_sessions.get_session_record_for_org(
        session_id,
        org_id=str(user.get("org_id")) if user.get("org_id") else None,
    )
    if not record:
        raise HTTPException(status_code=404, detail="Not found")
    return record


@router.post("/ideas/{idea_id}/browser/session", response_model=BrowserSessionRead)
async def create_browser_session(
    idea_id: str,
    payload: BrowserSessionCreate,
    user: dict[str, Any] = Depends(get_current_user),
):
    with UnitOfWork() as uow:
        if user.get("org_id") and not _validate_idea_org_orm(uow.session, idea_id, user.get("org_id")):
            raise HTTPException(status_code=404, detail="Not found")

    try:
        runtime = await browser_sessions.create_or_get_session(
            idea_id=idea_id,
            user_id=str(user.get("id")) if user.get("id") else None,
            url=payload.url,
            viewport_width=payload.viewport_width,
            viewport_height=payload.viewport_height,
            storage_mode=payload.storage_mode,
            allow_downloads=payload.allow_downloads,
            allow_file_uploads=payload.allow_file_uploads,
        )
    except BrowserCapabilityError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

    return BrowserSessionRead(
        id=runtime.session_id,
        idea_id=runtime.idea_id,
        user_id=runtime.user_id,
        run_id=runtime.run_id,
        status=runtime.status,
        current_url=runtime.current_url,
        page_title=runtime.page_title,
        viewport_width=runtime.viewport_width,
        viewport_height=runtime.viewport_height,
        storage_mode=runtime.storage_mode,
        allow_downloads=runtime.allow_downloads,
        allow_file_uploads=runtime.allow_file_uploads,
        last_error=runtime.last_error,
        active=runtime.status != "closed",
        last_frame_at=None,
        closed_at=None,
        created_at=_get_browser_session_or_404(runtime.session_id).created_at,
    )


@router.get("/ideas/{idea_id}/browser/session", response_model=BrowserSessionRead | None)
def get_browser_session(idea_id: str, user: dict[str, Any] = Depends(get_current_user)):
    with UnitOfWork() as uow:
        if user.get("org_id") and not _validate_idea_org_orm(uow.session, idea_id, user.get("org_id")):
            raise HTTPException(status_code=404, detail="Not found")
    record = browser_sessions.get_active_session_record(idea_id)
    if record is None:
        return None
    return BrowserSessionRead.model_validate(record)


@router.post("/browser/session/{session_id}/snapshot")
async def snapshot_browser_session(
    session_id: str,
    payload: dict[str, Any] | None = None,
    user: dict[str, Any] = Depends(get_current_user),
):
    _get_browser_session_for_user_or_404(session_id, user)
    try:
        return await browser_sessions.command(
            session_id,
            "snapshot",
            {
                "persist": bool((payload or {}).get("persist")),
                "title": (payload or {}).get("title"),
            },
        )
    except BrowserCapabilityError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e


@router.delete("/browser/session/{session_id}")
async def close_browser_session(session_id: str, user: dict[str, Any] = Depends(get_current_user)):
    _get_browser_session_for_user_or_404(session_id, user)
    await browser_sessions.command(session_id, "close", {"reason": "user_closed"})
    return {"closed": True, "session_id": session_id}
