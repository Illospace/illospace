from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from brain.systems.cycles.common import (
    MAX_CYCLE_TIMEOUT_SECONDS,
    MIN_CYCLE_TIMEOUT_SECONDS,
)


class CycleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=500)
    prompt: str = Field(min_length=1, max_length=20000)
    schedule_expr: str | None = Field(default=None, min_length=1, max_length=100)
    run_at: datetime | None = None
    timezone: str = Field(min_length=1, max_length=64)
    enabled: bool = True
    max_concurrency: int = Field(default=1, ge=1, strict=True)
    timeout_seconds: int | None = Field(
        default=None,
        ge=MIN_CYCLE_TIMEOUT_SECONDS,
        le=MAX_CYCLE_TIMEOUT_SECONDS,
        strict=True,
    )
    model_override: str | None = None
    thinking_override: str | None = None
    target_idea_id: str | None = None
    guidance: str | None = Field(default=None, max_length=20000)
    rationale: str | None = Field(default=None, max_length=5000)


class CycleUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=500)
    prompt: str | None = Field(default=None, min_length=1, max_length=20000)
    schedule_expr: str | None = Field(default=None, min_length=1, max_length=100)
    run_at: datetime | None = None
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    enabled: bool | None = None
    max_concurrency: int = Field(default=1, ge=1, strict=True)
    timeout_seconds: int | None = Field(
        default=None,
        ge=MIN_CYCLE_TIMEOUT_SECONDS,
        le=MAX_CYCLE_TIMEOUT_SECONDS,
        strict=True,
    )
    model_override: str | None = None
    thinking_override: str | None = None
    target_idea_id: str | None = None
    guidance: str | None = Field(default=None, max_length=20000)
    rationale: str | None = Field(default=None, max_length=5000)


class CycleRead(BaseModel):
    id: int
    user_id: str
    org_id: str | None = None
    workspace_id: str | None = None
    creator_type: str
    creator_id: str | None = None
    maintainer_type: str
    maintainer_id: str | None = None
    name: str
    prompt: str
    schedule_expr: str
    schedule_human: str
    timezone: str
    enabled: bool
    max_concurrency: int
    timeout_seconds: int | None = None
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
    revision_id: int | None = None
    scheduled_for: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    status: str
    error: str | None = None
    skip_reason: str | None = None
    idea_id: str | None = None
    run_id: int | None = None
    prompt_snapshot: str
    guidance_snapshot: list = Field(default_factory=list)
    output_targets_snapshot: list = Field(default_factory=list)
    context_snapshot: dict = Field(default_factory=dict)
    self_review_summary: str | None = None
    created_at: datetime
