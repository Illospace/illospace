from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class WorkspacePinRead(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    org_id: str
    label: str
    color: str
    position_x: float
    position_y: float
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_by_user_id: str | None = None
    archived_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class WorkspacePinCreate(BaseModel):
    label: str = Field(default="New Pin", min_length=1, max_length=160)
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    position_x: float
    position_y: float
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkspacePinUpdate(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=160)
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")
    position_x: float | None = None
    position_y: float | None = None
    metadata: dict[str, Any] | None = None
