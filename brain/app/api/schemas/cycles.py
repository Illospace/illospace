from __future__ import annotations

from dataclasses import fields
from datetime import datetime
from typing import Any, get_args

from pydantic import BaseModel, ConfigDict, Field, create_model

from brain.systems.cycles.behavior_policy_contract import CyclePolicySnapshot
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
    execution_policy_key: str | None = Field(default=None, max_length=100)
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
    execution_policy_key: str | None = Field(default=None, max_length=100)
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
    execution_policy_key: str | None = None
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


class CyclePolicyProposal(BaseModel):
    """The fields the first behavior-editor slice can change."""

    model_config = ConfigDict(extra="forbid")

    prompt: str | None = Field(default=None, min_length=1, max_length=20000)
    schedule_expr: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
    )
    timezone: str | None = Field(default=None, min_length=1, max_length=64)
    enabled: bool | None = None
    model_override: str | None = None
    thinking_override: str | None = None
    guidance: list[str] | None = None


class CyclePolicyPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal: CyclePolicyProposal


class CyclePolicyApplyRequest(CyclePolicyPreviewRequest):
    expected_version: int = Field(ge=0, strict=True)
    preview_digest: str = Field(min_length=1, max_length=128)
    rationale: str = Field(min_length=1, max_length=5000)


class CyclePolicyRevertApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=0, strict=True)
    preview_digest: str = Field(min_length=1, max_length=128)
    rationale: str = Field(min_length=1, max_length=5000)


def _cycle_policy_configuration_read_model(
    snapshot_type: type[CyclePolicySnapshot],
) -> type[BaseModel]:
    field_types = snapshot_type.configuration_field_types()
    model_fields: dict[str, tuple[Any, Any]] = {}
    for snapshot_field in fields(snapshot_type):
        if snapshot_field.name == "guidance":
            continue
        annotation = field_types[snapshot_field.name]
        default = None if type(None) in get_args(annotation) else ...
        model_fields[snapshot_field.name] = (annotation, default)
        if snapshot_field.name == "schedule_expr":
            model_fields["schedule_human"] = (str, ...)
    return create_model(
        "CyclePolicyConfigurationRead",
        __module__=__name__,
        **model_fields,
    )


CyclePolicyConfigurationRead = _cycle_policy_configuration_read_model(
    CyclePolicySnapshot
)


class CyclePolicySnapshotRead(BaseModel):
    configuration: CyclePolicyConfigurationRead
    guidance: list[str]


class CyclePolicyOutputTargetRead(BaseModel):
    id: int
    target_type: str
    target_id: str | None = None
    label: str | None = None
    config: dict[str, Any]
    source_type: str
    source_id: str | None = None
    rationale: str | None = None
    created_at: datetime
    updated_at: datetime


class CyclePolicySourceRead(BaseModel):
    revision_id: int | None = None
    actor_type: str | None = None
    actor_id: str | None = None
    rationale: str | None = None
    source_reference: str | None = None
    changed_at: datetime | None = None


class CyclePolicyFieldSourceRead(BaseModel):
    version: int
    cycle_revision_id: int | None = None
    actor_type: str | None = None
    actor_id: str | None = None
    source_reference: str | None = None
    rationale: str | None = None
    changed_at: datetime | None = None
    change_id: int | None = None


class CyclePolicyChangeSummaryRead(BaseModel):
    id: int
    version: int
    actor_type: str
    actor_id: str
    source_reference: str
    rationale: str
    changed_fields: list[str]
    applied_at: datetime
    reverted_from_id: int | None = None


class EffectiveCyclePolicyRead(BaseModel):
    workspace_id: str
    policy_kind: str
    target_type: str
    target_id: str
    version: int
    revision_id: int | None = None
    configuration: CyclePolicyConfigurationRead
    guidance: list[str]
    editable_fields: list[str]
    output_targets: list[CyclePolicyOutputTargetRead]
    output_targets_read_only: bool
    source: CyclePolicySourceRead
    field_sources: dict[str, CyclePolicyFieldSourceRead]
    latest_change: CyclePolicyChangeSummaryRead | None = None


class CyclePolicyDiffEntryRead(BaseModel):
    field: str
    kind: str
    before: Any
    after: Any
    added: list[str] | None = None
    removed: list[str] | None = None


class CyclePolicyWarningRead(BaseModel):
    code: str
    message: str


class CyclePolicyAffectedRunsRead(BaseModel):
    admitted_runs: str
    future_runs: str


class CyclePolicyPreviewRead(BaseModel):
    expected_version: int
    preview_digest: str
    before: CyclePolicySnapshotRead
    after: CyclePolicySnapshotRead
    changed_fields: list[str]
    diff: list[CyclePolicyDiffEntryRead]
    warnings: list[CyclePolicyWarningRead]
    affected_runs: CyclePolicyAffectedRunsRead
    reverted_from_id: int | None = None


class CyclePolicyChangeRead(CyclePolicyChangeSummaryRead):
    workspace_id: str
    policy_kind: str
    target_type: str
    target_id: str
    before_snapshot: CyclePolicySnapshotRead
    after_snapshot: CyclePolicySnapshotRead
    cycle_revision_id: int


class CyclePolicyApplyRead(BaseModel):
    effective_policy: EffectiveCyclePolicyRead
    change: CyclePolicyChangeRead


class CyclePolicyHistoryPaginationRead(BaseModel):
    limit: int
    offset: int
    has_more: bool
    next_offset: int | None = None


class CyclePolicyHistoryRead(BaseModel):
    items: list[CyclePolicyChangeRead]
    pagination: CyclePolicyHistoryPaginationRead
