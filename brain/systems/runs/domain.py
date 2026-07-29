"""Pure agent-run domain objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

from brain.systems.runs.status import RunStatus


class RunProfile(StrEnum):
    FAST = "fast"
    DEEP = "deep"


class RunRecipe(StrEnum):
    FAST = "fast"
    DEEP = "deep"
    SCOUT = "scout"
    WORKER = "worker"


class ArtifactType(StrEnum):
    FINAL_ANSWER = "final_answer"
    FILE_OBSERVATION = "file_observation"
    FILE_EDIT = "file_edit"
    COMMAND_OUTPUT = "command_output"
    PR_LINK = "pr_link"
    VERIFIER_EVIDENCE = "verifier_evidence"
    CONTEXT_PACK = "context_pack"
    SCOUT_HANDOFF = "scout_handoff"
    DEEP_PLAN = "deep_plan"
    DEEP_PLAN_REVISION = "deep_plan_revision"
    PHASE_RESULT = "phase_result"
    WORKER_RESULT = "worker_result"
    COST_SUMMARY = "cost_summary"


class EventVisibility(StrEnum):
    PUBLIC = "public"
    INTERNAL = "internal"
    SECRET = "secret"


@dataclass(frozen=True)
class AgentRunRequest:
    thread_id: str
    message: str
    org_id: str
    user_id: str | None = None
    profile: RunProfile | str = RunProfile.FAST
    recipe: RunRecipe | str | None = None
    parent_run_id: int | None = None
    root_run_id: int | None = None
    target_ref: dict[str, Any] = field(default_factory=dict)
    workspace_ref: dict[str, Any] = field(default_factory=dict)
    model_policy: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    deadline_at: datetime | None = None

    def __post_init__(self) -> None:
        org_id = str(self.org_id or "").strip()
        if not org_id:
            raise ValueError("AgentRun requires workspace org_id")
        object.__setattr__(self, "org_id", org_id)
        if self.deadline_at is not None and self.deadline_at.tzinfo is None:
            object.__setattr__(
                self,
                "deadline_at",
                self.deadline_at.replace(tzinfo=timezone.utc),
            )

    @property
    def normalized_profile(self) -> RunProfile:
        return _coerce_enum(RunProfile, self.profile, RunProfile.FAST)

    @property
    def normalized_recipe(self) -> RunRecipe:
        if self.recipe is not None:
            return _coerce_enum(RunRecipe, self.recipe, RunRecipe.FAST)
        return RunRecipe.FAST


@dataclass(frozen=True)
class AgentRun:
    id: int
    trace_id: str
    thread_id: str
    input_message: str
    profile: RunProfile
    recipe: RunRecipe
    status: RunStatus
    org_id: str | None = None
    user_id: str | None = None
    parent_run_id: int | None = None
    root_run_id: int | None = None
    target_ref: dict[str, Any] = field(default_factory=dict)
    workspace_ref: dict[str, Any] = field(default_factory=dict)
    model_policy: dict[str, Any] = field(default_factory=dict)
    context_summary: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    started_at: datetime | None = None
    deadline_at: datetime | None = None
    closeout_expires_at: datetime | None = None
    expired_at: datetime | None = None
    paused_at: datetime | None = None
    completed_at: datetime | None = None
    failed_at: datetime | None = None
    canceled_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class AgentRunEvent:
    run_id: int
    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    root_run_id: int | None = None
    sequence_no: int | None = None
    producer: str = "agent_runtime"
    visibility: EventVisibility = EventVisibility.PUBLIC
    created_at: datetime | None = None
    id: int | None = None


@dataclass(frozen=True)
class AgentRunArtifact:
    run_id: int
    artifact_type: ArtifactType | str
    title: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    text: str | None = None
    uri: str | None = None
    root_run_id: int | None = None
    visibility: EventVisibility = EventVisibility.PUBLIC
    created_at: datetime | None = None
    id: int | None = None

    @property
    def normalized_type(self) -> ArtifactType:
        return _coerce_enum(ArtifactType, self.artifact_type, ArtifactType.FINAL_ANSWER)


def _coerce_enum(enum_cls: type[Any], value: Any, default: Any) -> Any:
    if isinstance(value, enum_cls):
        return value
    candidate = str(value or "").strip().lower()
    for item in enum_cls:
        if candidate == item.value:
            return item
    return default

__all__ = [
    "AgentRun",
    "AgentRunArtifact",
    "AgentRunEvent",
    "AgentRunRequest",
    "ArtifactType",
    "EventVisibility",
    "RunProfile",
    "RunRecipe",
]
