from __future__ import annotations
from datetime import datetime
from uuid import UUID
from pydantic import BaseModel, Field, field_serializer

class SecretRead(BaseModel):
    id: int
    key_name: str
    description: str
    category: str
    created_at: datetime
    updated_at: datetime
    last_accessed_at: datetime | None = None
    access_count: int
    agent_access_level: str = "ask"
    user_id: str | UUID
    org_id: str | UUID | None = None
    is_shared: bool = False
    shared_by_name: str | None = None
    model_config = {"from_attributes": True}

    @field_serializer("user_id", "org_id")
    @classmethod
    def serialize_uuid(cls, v: object) -> str | None:
        return str(v) if v is not None else None

class SecretCreate(BaseModel):
    key_name: str = Field(min_length=1, max_length=128)
    value: str = Field(min_length=1)
    description: str = ""
    category: str = "general"
    agent_access_level: str = Field(default="ask", pattern="^(available|ask|manual)$")

class SecretReveal(BaseModel):
    key_name: str
    value: str

class VaultShareCreate(BaseModel):
    shared_with_user_id: str


class VaultProjectBindingCreate(BaseModel):
    project_slug: str = Field(min_length=1, max_length=120)
    env_name: str = Field(min_length=1, max_length=128)
    target_registry_id: int | None = None


class VaultProjectBindingRead(BaseModel):
    id: int
    secret_id: int
    key_name: str | None = None
    agent_access_level: str | None = None
    user_id: str | UUID
    org_id: str | UUID | None = None
    target_registry_id: int | None = None
    project_slug: str
    env_name: str
    active: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None
    model_config = {"from_attributes": True}

    @field_serializer("user_id", "org_id")
    @classmethod
    def serialize_ids(cls, v: object) -> str | None:
        return str(v) if v is not None else None

class VaultShareRead(BaseModel):
    id: int
    secret_id: int
    shared_with_user_id: str | UUID
    shared_by_user_id: str | UUID
    shared_at: datetime | None = None
    model_config = {"from_attributes": True}

    @field_serializer("shared_with_user_id", "shared_by_user_id")
    @classmethod
    def serialize_uuid(cls, v: object) -> str:
        return str(v) if v is not None else None
