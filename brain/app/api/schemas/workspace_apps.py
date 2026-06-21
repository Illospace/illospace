from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class WorkspaceAppVersionRead(BaseModel):
    id: int
    app_id: str
    version: int
    renderer_key: str
    source_kind: str
    source_code: str
    manifest: dict[str, Any]
    created_by_user_id: str | None = None
    created_at: datetime


class WorkspaceAppRead(BaseModel):
    id: str
    org_id: str
    key: str
    name: str
    description: str | None = None
    renderer_key: str
    visual_spec: dict[str, Any]
    metadata: dict[str, Any]
    created_by_user_id: str | None = None
    anchor_user_id: str | None = None
    archived_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    active_version: WorkspaceAppVersionRead | None = None
    contract_validation: dict[str, Any] = Field(default_factory=dict)


class WorkspaceAppCreate(BaseModel):
    key: str | None = Field(default=None, max_length=100)
    name: str = Field(min_length=1, max_length=500)
    description: str | None = None
    renderer_key: str = Field(default="app-capsule", max_length=120)
    source_kind: str = Field(default="html", max_length=40)
    source_code: str = ""
    manifest: dict[str, Any] = Field(default_factory=dict)
    visual_spec: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    anchor_user_id: str | None = None
    initial_state: dict[str, Any] | None = None
    state_key: str = Field(default="default", max_length=120)


class WorkspaceAppUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = None
    renderer_key: str | None = Field(default=None, max_length=120)
    source_kind: str | None = Field(default=None, max_length=40)
    source_code: str | None = None
    manifest: dict[str, Any] | None = None
    visual_spec: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None
    anchor_user_id: str | None = None


class WorkspaceAppStateRead(BaseModel):
    id: int
    org_id: str
    app_id: str
    scope: str
    key: str
    data: dict[str, Any]
    version: int = 0
    updated_by_user_id: str | None = None
    created_at: datetime
    updated_at: datetime


class WorkspaceAppStateUpdate(BaseModel):
    data: dict[str, Any] = Field(default_factory=dict)
    data_patch: dict[str, Any] | None = None


class WorkspaceAppEventCreate(BaseModel):
    event_type: str = Field(min_length=1, max_length=120)
    payload: dict[str, Any] = Field(default_factory=dict)
    state_patch: dict[str, Any] | None = None
    state_key: str | None = Field(default=None, max_length=120)
    idempotency_key: str | None = Field(default=None, max_length=180)
    expected_state_version: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class WorkspaceAppEventRead(BaseModel):
    id: int
    org_id: str
    app_id: str
    thread_id: str | None = None
    event_type: str
    idempotency_key: str | None = None
    actor_kind: str
    actor_user_id: str | None = None
    actor_display: dict[str, Any]
    payload: dict[str, Any]
    state_key: str
    state_patch: dict[str, Any]
    state_version: int
    metadata: dict[str, Any]
    created_at: datetime


class WorkspaceAppEventsRead(BaseModel):
    events: list[WorkspaceAppEventRead] = Field(default_factory=list)


class WorkspaceAppCollaborationRead(BaseModel):
    app_id: str
    state: WorkspaceAppStateRead
    events: list[WorkspaceAppEventRead] = Field(default_factory=list)
    collaboration: dict[str, Any] = Field(default_factory=dict)
    duplicate: bool = False


class WorkspaceAppActionRun(BaseModel):
    action_key: str = Field(min_length=1, max_length=160)
    payload: dict[str, Any] = Field(default_factory=dict)


class WorkspaceAppActionRunRead(BaseModel):
    ok: bool
    action_key: str
    status: str
    effects: list[str] = Field(default_factory=list)
    connector_keys: list[str] = Field(default_factory=list)
    result: dict[str, Any] = Field(default_factory=dict)


class WorkspaceAppBindingRun(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)


class WorkspaceAppBindingRunRead(BaseModel):
    ok: bool
    alias: str
    operation: str
    kind: str
    data: Any = None
    warnings: list[str] = Field(default_factory=list)
