from __future__ import annotations

import json
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_serializer, model_validator


def validate_github_app_secret_value(value: str) -> None:
    try:
        payload = json.loads(value)
    except Exception:
        raise ValueError("github_app value must be valid JSON") from None
    if not isinstance(payload, dict):
        raise ValueError("github_app value must be a JSON object")
    for field in ("app_id", "installation_id", "private_key_pem"):
        raw_value = payload.get(field)
        if field == "private_key_pem":
            valid = isinstance(raw_value, str) and bool(raw_value.strip())
        else:
            valid = (
                raw_value is not None
                and raw_value is not True
                and raw_value is not False
                and bool(str(raw_value).strip())
            )
        if not valid:
            raise ValueError(f"github_app value requires non-empty {field}")
    private_key_pem = str(payload.get("private_key_pem") or "").strip()
    if not private_key_pem.startswith("-----BEGIN"):
        raise ValueError("github_app private_key_pem must start with a PEM header")


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
    org_id: str | UUID
    created_by_user_id: str | UUID | None = None
    updated_by_user_id: str | UUID | None = None
    model_config = {"from_attributes": True}

    @field_serializer("org_id", "created_by_user_id", "updated_by_user_id")
    @classmethod
    def serialize_uuid(cls, v: object) -> str | None:
        return str(v) if v is not None else None


class SecretCreate(BaseModel):
    key_name: str = Field(min_length=1, max_length=128)
    value: str = Field(min_length=1)
    description: str = ""
    category: str = "general"
    agent_access_level: str = Field(default="ask", pattern="^(available|ask|manual)$")
    model_config = {"hide_input_in_errors": True}

    @model_validator(mode="after")
    def validate_github_app_value(self) -> "SecretCreate":
        if self.category != "github_app":
            return self
        if self.agent_access_level != "manual":
            raise ValueError("github_app secrets must be stored with agent_access_level 'manual'")
        validate_github_app_secret_value(self.value)
        return self


class SecretReveal(BaseModel):
    key_name: str
    value: str


class VaultProjectBindingCreate(BaseModel):
    project_slug: str = Field(min_length=1, max_length=120)
    env_name: str = Field(min_length=1, max_length=128)
    target_registry_id: int | None = None


class VaultProjectBindingRead(BaseModel):
    id: int
    secret_id: int
    key_name: str | None = None
    agent_access_level: str | None = None
    org_id: str | UUID
    created_by_user_id: str | UUID | None = None
    target_registry_id: int | None = None
    project_slug: str
    env_name: str
    active: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None
    model_config = {"from_attributes": True}

    @field_serializer("org_id", "created_by_user_id")
    @classmethod
    def serialize_ids(cls, v: object) -> str | None:
        return str(v) if v is not None else None
