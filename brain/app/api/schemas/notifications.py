"""Pydantic schemas for the unified notification inbox."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class NotificationPreferencesRead(BaseModel):
    sound_enabled: bool = True
    message_notifications_enabled: bool = True


class NotificationPreferencesUpdate(BaseModel):
    sound_enabled: bool | None = None
    message_notifications_enabled: bool | None = None


class NotificationRead(BaseModel):
    id: int
    source: str
    kind: str
    title: str
    body: str | None = None
    actor_user_id: str | None = None
    actor_name: str | None = None
    actor_color: str | None = None
    idea_id: str | None = None
    conversation_id: str | None = None
    thread_root_message_id: int | None = None
    occurrence_count: int = 1
    payload: dict[str, Any] | None = None
    created_at: datetime
    updated_at: datetime
    read_at: datetime | None = None


class NotificationSummaryRead(BaseModel):
    chat_unread_total: int = 0
    workspace_attention_total: int = 0
    unread_notification_total: int = 0
    unread_chat_notification_total: int = 0
    unread_workspace_notification_total: int = 0
