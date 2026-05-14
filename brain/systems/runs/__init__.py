"""Canonical agent-run runtime."""

from dataclasses import dataclass
from typing import Any

from brain.systems.runs.domain import (
    AgentRun,
    AgentRunArtifact,
    AgentRunEvent,
    AgentRunRequest,
    ArtifactType,
    EventVisibility,
    RunProfile,
    RunRecipe,
)
from brain.systems.runs.engine import AsyncAgentRunEngine, RunRecipeResult, RunRuntime
from brain.systems.runs.execution_context import (
    AgentExecutionContext,
    bind_agent_context,
    current_agent_context,
    get_agent_context_value,
    snapshot_agent_context,
)
from brain.systems.runs.assignments import AcceptanceCriteria, EvidenceRequirement, WorkerAssignment
from brain.systems.runs.graph import (
    DeepPlan,
    RunEdge,
    RunGraph,
    RunGraphCycleError,
    RunGraphError,
    RunGraphMissingDependencyError,
    RunNode,
)
from brain.systems.runs.status import RunStatus
from brain.systems.runs.tool_surface import build_agent_tools, build_tool_handlers


@dataclass(frozen=True)
class RunProfilePolicy:
    profile: RunProfile
    stream_live_reply: bool
    allow_lazy_skills: bool
    requires_run_graph: bool


@dataclass(frozen=True)
class RunRuntimeSelection:
    profile: RunProfile
    run_graph: bool
    stream_live_reply: bool


def requested_run_profile(metadata: dict[str, Any] | None) -> str:
    metadata = metadata or {}
    value = (
        metadata.get("requested_run_profile")
        or metadata.get("executionProfile")
        or metadata.get("execution_profile")
        or metadata.get("run_profile")
        or metadata.get("profile")
        or RunProfile.FAST.value
    )
    try:
        return RunProfile(str(value).strip().lower()).value
    except Exception:
        return RunProfile.FAST.value


def run_execution_profile(metadata: dict[str, Any] | None) -> str:
    return requested_run_profile(metadata)


def run_profile_policy(profile: RunProfile | str) -> RunProfilePolicy:
    try:
        normalized = RunProfile(str(profile).strip().lower())
    except Exception:
        normalized = RunProfile.FAST
    if normalized is RunProfile.DEEP:
        return RunProfilePolicy(
            profile=normalized,
            stream_live_reply=False,
            allow_lazy_skills=True,
            requires_run_graph=True,
        )
    return RunProfilePolicy(
        profile=RunProfile.FAST,
        stream_live_reply=True,
        allow_lazy_skills=True,
        requires_run_graph=False,
    )


def select_run_runtime(policy: RunProfilePolicy | RunProfile | str) -> RunRuntimeSelection:
    profile_policy = policy if isinstance(policy, RunProfilePolicy) else run_profile_policy(policy)
    return RunRuntimeSelection(
        profile=profile_policy.profile,
        run_graph=profile_policy.requires_run_graph,
        stream_live_reply=profile_policy.stream_live_reply,
    )

__all__ = [
    "AcceptanceCriteria",
    "AgentRun",
    "AgentRunArtifact",
    "AsyncAgentRunEngine",
    "AgentRunEvent",
    "AgentRunRequest",
    "AgentExecutionContext",
    "ArtifactType",
    "DeepPlan",
    "EvidenceRequirement",
    "EventVisibility",
    "RunProfile",
    "RunRecipe",
    "RunRecipeResult",
    "RunRuntime",
    "RunProfilePolicy",
    "RunRuntimeSelection",
    "RunEdge",
    "RunGraph",
    "RunGraphCycleError",
    "RunGraphError",
    "RunGraphMissingDependencyError",
    "RunNode",
    "RunStatus",
    "WorkerAssignment",
    "bind_agent_context",
    "build_agent_tools",
    "build_tool_handlers",
    "current_agent_context",
    "get_agent_context_value",
    "requested_run_profile",
    "run_execution_profile",
    "run_profile_policy",
    "select_run_runtime",
    "snapshot_agent_context",
]
