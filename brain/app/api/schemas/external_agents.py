from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class ExternalAgentConnectionCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=200)
    agent_kind: str = Field(default="custom", max_length=40)
    transport: str = Field(default="bridge_pull", max_length=60)
    endpoint_url: str | None = None
    remote_agent_id: str | None = None
    remote_agent_card: dict[str, Any] = Field(default_factory=dict)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExternalAgentConnectionRead(BaseModel):
    id: str
    org_id: str
    owner_user_id: str
    display_name: str
    agent_kind: str
    transport: str
    status: str
    endpoint_url: str | None = None
    remote_agent_id: str | None = None
    remote_session_key: str | None = None
    remote_agent_card: dict[str, Any] = Field(default_factory=dict)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    last_seen_at: str | None = None
    last_tested_at: str | None = None
    last_error: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    disabled_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class ExternalAgentTokenCreate(BaseModel):
    name: str = Field(default="Bridge token", max_length=120)
    scopes: list[str] | None = None
    expires_at: datetime | None = None


class ExternalAgentTokenRead(BaseModel):
    id: str
    connection_id: str
    token_prefix: str
    name: str
    scopes: list[str]
    created_at: str | None = None
    last_used_at: str | None = None
    expires_at: str | None = None
    revoked_at: str | None = None
    token: str | None = None


class CortexExternalAgentTaskCreate(BaseModel):
    connection_id: str
    instructions: str = Field(min_length=1)
    title: str | None = None
    include_thread_context: bool = True
    include_project_context: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = Field(default=None, max_length=80)


class BridgeHeartbeatRequest(BaseModel):
    status: str | None = None
    capabilities: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None


class BridgeClaimTasksRequest(BaseModel):
    max_tasks: int = Field(default=1, ge=1, le=10)


class BridgeTaskEventRequest(BaseModel):
    event_type: str = Field(min_length=1, max_length=120)
    status: str | None = None
    message: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    remote_event_id: str | None = None


class BridgeArtifactCreate(BaseModel):
    kind: str = Field(default="text", max_length=40)
    title: str | None = None
    mime_type: str | None = None
    content_text: str | None = None
    content_json: dict[str, Any] | None = None
    uri: str | None = None
    upload_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class BridgeCompleteTaskRequest(BaseModel):
    result_summary: str = Field(min_length=1)
    artifacts: list[BridgeArtifactCreate] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)


class BridgeFailTaskRequest(BaseModel):
    error: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)


class BridgeWorkspaceSearchRequest(BaseModel):
    query: str = Field(min_length=1)
    limit: int = Field(default=10, ge=1, le=25)


class BridgeAskIlloRequest(BaseModel):
    question: str = Field(min_length=1)
    context: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class BridgeThreadCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1)
    teammate_user_ids: list[str] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    trigger_illo: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("teammate_user_ids", mode="before")
    @classmethod
    def normalize_teammates(cls, value):
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item) for item in value if str(item or "").strip()]
        return [str(value)] if str(value or "").strip() else []


class BridgeThreadMessageCreateRequest(BaseModel):
    body: str = Field(min_length=1)
    teammate_user_ids: list[str] = Field(default_factory=list)
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    trigger_illo: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("teammate_user_ids", mode="before")
    @classmethod
    def normalize_teammates(cls, value):
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item) for item in value if str(item or "").strip()]
        return [str(value)] if str(value or "").strip() else []
