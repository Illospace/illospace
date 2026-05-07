"""Unified notification inbox API."""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from brain.app.api.auth import get_current_user
from brain.app.api.deps import get_db, rate_limit
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
def get_notification_summary(
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    return NotificationService(db, user).summary()


@router.get("/preferences", response_model=NotificationPreferencesRead)
def get_notification_preferences(
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    return NotificationService(db, user).preferences()


@router.patch("/preferences", response_model=NotificationPreferencesRead)
def update_notification_preferences(
    body: NotificationPreferencesUpdate,
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    service = NotificationService(db, user)
    preferences = service.update_preferences(body)
    db.commit()
    return preferences


@router.get("", response_model=list[NotificationRead])
def list_notifications(
    status: str = Query(default="unread", pattern="^(unread|all)$"),
    limit: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    return NotificationService(db, user).list_notifications(
        unread_only=status != "all",
        limit=limit,
    )


@router.post("/{notification_id}/read", response_model=NotificationSummaryRead)
async def mark_notification_read(
    notification_id: int,
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    service = NotificationService(db, user)
    summary = service.mark_read(notification_id)
    db.commit()
    try:
        await _publish_notification_summary(user_id=str(user["id"]), summary=summary)
    except Exception as exc:
        logger.warning("notification_summary_publish_failed: %s", exc)
    return summary


@router.post("/read-all", response_model=NotificationSummaryRead)
async def mark_all_notifications_read(
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    service = NotificationService(db, user)
    summary = service.mark_all_read()
    db.commit()
    try:
        await _publish_notification_summary(user_id=str(user["id"]), summary=summary)
    except Exception as exc:
        logger.warning("notification_summary_publish_failed: %s", exc)
    return summary
