from __future__ import annotations
from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, field_serializer

class TeamMemberRead(BaseModel):
    id: str | UUID
    name: str
    email: str
    role: str
    color: str | None = None
    cortex_color: str | None = None
    attribution_enabled: bool | None = True
    approved: bool | None = None
    created_at: datetime
    model_config = {"from_attributes": True}

    @field_serializer("id")
    @classmethod
    def serialize_id(cls, v: object) -> str:
        return str(v) if v is not None else None

    def model_post_init(self, __context: object) -> None:
        # cortex_color is an alias for color — frontend uses this for sun positioning
        if self.cortex_color is None and self.color is not None:
            self.cortex_color = self.color


class CortexColorRead(BaseModel):
    """Lightweight schema for frontend sun positioning."""
    id: str | UUID
    name: str
    cortex_color: str
    model_config = {"from_attributes": True}

    @field_serializer("id")
    @classmethod
    def serialize_id(cls, v: object) -> str:
        return str(v) if v is not None else None


class UserProfileUpdate(BaseModel):
    name: str | None = None
    color: str | None = None
    attribution_enabled: bool | None = None
    default_provider: str | None = None
