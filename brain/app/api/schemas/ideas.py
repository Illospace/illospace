from __future__ import annotations
from datetime import datetime
from typing import Any
from uuid import UUID
from pydantic import BaseModel, Field, field_serializer, field_validator


def _jsonish_or_none(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool, dict, list)):
        return value
    return None

class _UUIDMixin:
    """Serialize UUID fields to strings automatically."""
    model_config = {"from_attributes": True}

    @field_serializer('id', 'user_id', 'org_id', 'parent_id', 'idea_id', 'source_id', 'target_id', check_fields=False)
    @classmethod
    def serialize_uuid(cls, v):
        return str(v) if v is not None else None

class IdeaRead(_UUIDMixin, BaseModel):
    id: str | UUID
    title: str
    display_title: str | None = None
    description: str | None = None
    status: str
    origin: str
    origin_ref: str | None = None
    salience_score: float | None = 5.0
    position_x: float | None = None
    position_y: float | None = None
    position_sticky: bool = False
    parent_id: str | UUID | None = None
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None
    user_id: str | UUID | None = None
    org_id: str | UUID | None = None
    orbit_anchor_type: str | None = None
    orbit_anchor_id: str | UUID | None = None
    # Presentation hints used by the Cortex UI. Ownership/user_id controls access
    # and handoff; orbit_anchor_* controls where the blob orbits.
    author_name: str | None = None
    author_color: str | None = None
    project_context: dict | None = None
    thread_count: int | None = 0
    active_agents: int = 0
    agent_details: Any = None
    attachments: list[Any] = Field(default_factory=list)

    @field_validator(
        "display_title",
        "description",
        "origin_ref",
        "orbit_anchor_type",
        "orbit_anchor_id",
        "author_name",
        "author_color",
        mode="before",
    )
    @classmethod
    def coerce_optional_strings(cls, value):
        if value is None or isinstance(value, str):
            return value
        return None

    @field_validator("project_context", mode="before")
    @classmethod
    def coerce_optional_project_context(cls, value):
        return value if isinstance(value, dict) else None

    @field_validator("agent_details", mode="before")
    @classmethod
    def coerce_agent_details(cls, value):
        return _jsonish_or_none(value)

    @field_validator("attachments", mode="before")
    @classmethod
    def coerce_attachments(cls, value):
        return value if isinstance(value, list) else []


class IdeaCreate(BaseModel):
    title: str = Field(min_length=1, max_length=5000)
    description: str | None = None
    status: str = "emerged"
    origin: str = "user_created"
    origin_ref: str | None = None
    salience_score: float | None = None
    position_x: float | None = None
    position_y: float | None = None
    orbit_anchor_type: str | None = None
    orbit_anchor_id: str | None = None
    parent_id: str | None = None

class IdeaUpdate(BaseModel):
    title: str | None = None
    display_title: str | None = None
    description: str | None = None
    status: str | None = None
    salience_score: float | None = None
    position_x: float | None = None
    position_y: float | None = None
    position_sticky: bool | None = None
    orbit_anchor_type: str | None = None
    orbit_anchor_id: str | UUID | None = None
    # Changing user_id is an explicit thread handoff: ownership changes without
    # archiving and without changing the last-human-author color.
    user_id: str | UUID | None = None

class IdeaStatusUpdate(BaseModel):
    status: str = Field(
        pattern="^(emerged|queued|active|working|needs_input|unread_reply|blocked|failed|resolved|stale|paused|done|archived)$"
    )
    trigger: str | None = None

class ThreadMessageRead(_UUIDMixin, BaseModel):
    id: int
    idea_id: str | UUID | None = None
    role: str
    content: str
    user_id: str | UUID | None = None
    created_at: datetime

class ThreadMessageCreate(BaseModel):
    content: str = Field(min_length=1)
    role: str = "user"


class VisualBlockRead(BaseModel):
    model_config = {"from_attributes": True}
    id: int
    idea_id: str
    content_type: str
    title: str
    content: str
    display_mode: str = "inline"
    position_after: int | None = None
    run_id: str | None = None
    created_at: datetime


class BrowserSessionRead(BaseModel):
    model_config = {"from_attributes": True}
    id: str
    idea_id: str
    user_id: str | None = None
    run_id: int | None = None
    status: str
    current_url: str | None = None
    page_title: str | None = None
    viewport_width: int = 1280
    viewport_height: int = 800
    storage_mode: str = "ephemeral"
    allow_downloads: bool = False
    allow_file_uploads: bool = True
    last_error: str | None = None
    active: bool = True
    last_frame_at: datetime | None = None
    closed_at: datetime | None = None
    created_at: datetime


class BrowserSessionCreate(BaseModel):
    url: str | None = None
    viewport_width: int = Field(default=1280, ge=320, le=2560)
    viewport_height: int = Field(default=800, ge=240, le=1600)
    storage_mode: str = Field(default="ephemeral", pattern="^(ephemeral|idea)$")
    allow_downloads: bool = False
    allow_file_uploads: bool = True

class IdeaConnectionRead(_UUIDMixin, BaseModel):
    id: str | UUID
    source_id: str | UUID | None = None
    target_id: str | UUID | None = None
    type: str
    weight: float
    reason: str | None = None
    created_at: datetime
