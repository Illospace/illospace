from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class CycleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=500)
    prompt: str = Field(min_length=1, max_length=20000)
    schedule_expr: str | None = Field(default=None, min_length=1, max_length=100)
    run_at: datetime | None = None
    timezone: str = Field(min_length=1, max_length=64)
    enabled: bool = True
    model_override: str | None = None
    thinking_override: str | None = None
    execution_mode: str = "reuse_same_idea"
    target_idea_id: str | None = None
    reopen_archived: bool | None = None


class CycleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=500)
    prompt: str | None = Field(default=None, min_length=1, max_length=20000)
    schedule_expr: str | None = Field(default=None, min_length=1, max_length=100)
    run_at: datetime | None = None
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    enabled: bool | None = None
    model_override: str | None = None
    thinking_override: str | None = None
    execution_mode: str | None = None
    target_idea_id: str | None = None
    reopen_archived: bool | None = None


class CycleRead(BaseModel):
    id: int
    user_id: str
    org_id: str | None = None
    name: str
    prompt: str
    schedule_expr: str
    schedule_human: str
    timezone: str
    enabled: bool
    model_override: str | None = None
    thinking_override: str | None = None
    execution_mode: str
    target_idea_id: str | None = None
    reopen_archived: bool
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    last_status: str | None = None
    last_error: str | None = None
    created_at: datetime
    updated_at: datetime


class CycleRunRead(BaseModel):
    id: int
    cycle_id: int
    scheduled_for: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    status: str
    error: str | None = None
    skip_reason: str | None = None
    idea_id: str | None = None
    run_id: int | None = None
    prompt_snapshot: str
    created_at: datetime
