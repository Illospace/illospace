"""Unified notification inbox API."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from brain.app.api.auth import get_current_user
from brain.app.api.deps import get_db, rate_limit
from brain.app.api.db_utils import run_db
from brain.app.api.schemas.notifications import (
    NotificationPreferencesRead,
    NotificationPreferencesUpdate,
    NotificationRead,
    NotificationSummaryRead,
)
from brain.app.api.services.notifications import NotificationService

router = APIRouter(
    prefix="/api/notifications",
    tags=["notifications"],
    dependencies=[Depends(rate_limit)],
)
logger = logging.getLogger(__name__)


async def _publish_notification_summary(
    *,
    user_id: str,
    summary: NotificationSummaryRead,
) -> None:
    from brain.app.api.routers.ws import ws_manager

    await ws_manager.publish_notification_summary_updated(
        user_id=user_id,
        summary=summary.model_dump(mode="json"),
    )


@router.get("/summary", response_model=NotificationSummaryRead)
async def get_notification_summary(
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    return await run_db(db, lambda sync_db: NotificationService(sync_db, user).summary())


@router.get("/preferences", response_model=NotificationPreferencesRead)
async def get_notification_preferences(
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    return await run_db(db, lambda sync_db: NotificationService(sync_db, user).preferences())


@router.patch("/preferences", response_model=NotificationPreferencesRead)
async def update_notification_preferences(
    body: NotificationPreferencesUpdate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _update(sync_db: Session):
        service = NotificationService(sync_db, user)
        preferences = service.update_preferences(body)
        sync_db.commit()
        return preferences

    return await run_db(db, _update)


@router.get("", response_model=list[NotificationRead])
async def list_notifications(
    status: str = Query(default="unread", pattern="^(unread|all)$"),
    limit: int = Query(default=50, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    return await run_db(
        db,
        lambda sync_db: NotificationService(sync_db, user).list_notifications(
            unread_only=status != "all",
            limit=limit,
        )
    )


@router.post("/{notification_id}/read", response_model=NotificationSummaryRead)
async def mark_notification_read(
    notification_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _mark(sync_db: Session):
        service = NotificationService(sync_db, user)
        summary = service.mark_read(notification_id)
        sync_db.commit()
        return summary

    summary = await run_db(db, _mark)
    try:
        await _publish_notification_summary(user_id=str(user["id"]), summary=summary)
    except Exception as exc:
        logger.warning("notification_summary_publish_failed: %s", exc)
    return summary


@router.post("/read-all", response_model=NotificationSummaryRead)
async def mark_all_notifications_read(
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _mark(sync_db: Session):
        service = NotificationService(sync_db, user)
        summary = service.mark_all_read()
        sync_db.commit()
        return summary

    summary = await run_db(db, _mark)
    try:
        await _publish_notification_summary(user_id=str(user["id"]), summary=summary)
    except Exception as exc:
        logger.warning("notification_summary_publish_failed: %s", exc)
    return summary
