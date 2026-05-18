"""Pydantic schemas for the native chat backend."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ChatParticipantRead(BaseModel):
    id: str
    name: str
    color: str | None = None
    email: str | None = None


class ChatMessageRead(BaseModel):
    id: int
    conversation_id: str
    sender_user_id: str | None = None
    sender_kind: str
    sender_name: str
    sender_color: str | None = None
    body: str
    body_format: str
    client_generated_id: str | None = None
    thread_root_message_id: int | None = None
    reply_to_message_id: int | None = None
    attachments: list[Any] = Field(default_factory=list)
    metadata: dict[str, Any] | None = None
    conversation_seq: int
    reply_count: int = 0
    last_reply_at: datetime | None = None
    last_reply_message_id: int | None = None
    thread_preview_participants: list[ChatParticipantRead] = Field(default_factory=list)
    created_at: datetime
    edited_at: datetime | None = None
    deleted_at: datetime | None = None


class ChatConversationSummaryRead(BaseModel):
    id: str
    type: str
    stable_key: str
    title: str | None = None
    description: str | None = None
    visibility: str
    last_message_seq: int = 0
    unread_count: int = 0
    participant_count: int = 0
    counterpart: ChatParticipantRead | None = None
    last_message: ChatMessageRead | None = None
    created_at: datetime
    updated_at: datetime


class ChatUnreadSummaryRead(BaseModel):
    room: int = 0
    dms: int = 0
    total: int = 0


class ChatNotificationRead(BaseModel):
    id: int
    type: str
    conversation_id: str | None = None
    message_id: int | None = None
    actor_user_id: str | None = None
    actor_name: str | None = None
    actor_color: str | None = None
    metadata: dict[str, Any] | None = None
    created_at: datetime
    read_at: datetime | None = None


class ChatUnreadThreadRead(BaseModel):
    kind: str
    conversation: ChatConversationSummaryRead
    root_message: ChatMessageRead | None = None
    unread_messages: list[ChatMessageRead] = Field(default_factory=list)
    notification_ids: list[int] = Field(default_factory=list)
    unread_count: int = 0
    latest_unread_at: datetime


class ChatBootstrapRead(BaseModel):
    room: ChatConversationSummaryRead
    dms: list[ChatConversationSummaryRead]
    notifications: list[ChatNotificationRead]
    unread_summary: ChatUnreadSummaryRead
    default_mode: str = "room"
    default_conversation_id: str


class ChatMessagePageRead(BaseModel):
    conversation: ChatConversationSummaryRead
    messages: list[ChatMessageRead]
    has_more: bool = False
    next_before_seq: int | None = None


class ChatThreadRead(BaseModel):
    conversation: ChatConversationSummaryRead
    root_message: ChatMessageRead
    replies: list[ChatMessageRead]
    has_more: bool = False
    next_before_seq: int | None = None


class ChatSearchResultRead(BaseModel):
    message: ChatMessageRead
    root_message: ChatMessageRead


class ChatMessageCreate(BaseModel):
    body: str = Field(default="", max_length=12000)
    body_format: str = Field(default="markdown", pattern="^(markdown|plain)$")
    client_generated_id: str | None = Field(default=None, max_length=255)
    attachments: list[Any] = Field(default_factory=list)
    reply_to_message_id: int | None = None
    metadata: dict[str, Any] | None = None


class ChatDmCreate(BaseModel):
    user_id: str = Field(min_length=1)


class ChatReadUpdate(BaseModel):
    last_read_message_id: int | None = None
    last_read_conversation_seq: int | None = Field(default=None, ge=0)
