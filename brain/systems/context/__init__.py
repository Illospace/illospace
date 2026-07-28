"""Context runtime wrappers over AgentRun context packs."""

from typing import Any

ContextPack = dict[str, Any]
ContextSection = dict[str, Any]
ContextSectionName = str
ContextTokenBudget = dict[str, Any]
OmittedContextSection = dict[str, Any]

from brain.systems.context.sections import validate_context_pack
from brain.systems.context.budget import ModelContextBudget, resolve_model_context_budget
from brain.systems.context.compaction import (
    CompactionReport,
    CompactionWindow,
    compact_session_messages,
    split_session_messages_for_compaction,
)
from brain.systems.context.semantic_compaction import (
    CompactionPlan,
    CompactionCheckpoint,
    plan_session_compaction,
)
from brain.systems.context.errors import (
    ContextCompactionStalledError,
    ContextFloorExceedsBudgetError,
)
from brain.systems.context.window_policy import (
    ContextAdmission,
    ContextCompactionOutcome,
    ContextWindowPolicy,
)
from brain.systems.context.thread_handoff import (
    ThreadHandoff,
    build_thread_handoff,
    build_thread_handoff_context_messages,
    recent_raw_messages_for_handoff,
    thread_handoff_message,
)
from brain.systems.context.policy import (
    ActiveContextPolicyApplication,
    CONTEXT_POLICY_ACTIVE_FLAG,
    CONTEXT_POLICY_GLOBAL_ACTIVE_FLAG,
    CONTEXT_POLICY_GLOBAL_DISABLED_FLAG,
    CONTEXT_POLICY_VERSION,
    ContextPolicyAction,
    ContextPolicyDecisionRecord,
    ContextTaskClass,
    MemoryFreshnessStatus,
    MemoryPolicyInput,
    MemoryTruthStatus,
    SectionUsefulnessHistory,
    apply_active_context_policy,
    context_policy_active_enabled,
    normalize_task_class,
)
from brain.systems.context.runtime import ContextRender, ContextRuntime
from brain.systems.context.sections import (
    COORDINATOR_SECTION_ORDER,
    WORKER_SECTION_ORDER,
    filter_context_pack_sections,
)

__all__ = [
    "COORDINATOR_SECTION_ORDER",
    "ActiveContextPolicyApplication",
    "CONTEXT_POLICY_ACTIVE_FLAG",
    "CONTEXT_POLICY_GLOBAL_ACTIVE_FLAG",
    "CONTEXT_POLICY_GLOBAL_DISABLED_FLAG",
    "CONTEXT_POLICY_VERSION",
    "CompactionReport",
    "CompactionWindow",
    "CompactionPlan",
    "CompactionCheckpoint",
    "ContextAdmission",
    "ContextCompactionOutcome",
    "ContextCompactionStalledError",
    "ContextFloorExceedsBudgetError",
    "ContextWindowPolicy",
    "ContextRender",
    "ContextRuntime",
    "ContextPack",
    "ContextPolicyAction",
    "ContextPolicyDecisionRecord",
    "ContextSection",
    "ContextSectionName",
    "ContextTaskClass",
    "ContextTokenBudget",
    "MemoryFreshnessStatus",
    "MemoryPolicyInput",
    "MemoryTruthStatus",
    "ModelContextBudget",
    "OmittedContextSection",
    "SectionUsefulnessHistory",
    "ThreadHandoff",
    "WORKER_SECTION_ORDER",
    "apply_active_context_policy",
    "compact_session_messages",
    "context_policy_active_enabled",
    "filter_context_pack_sections",
    "build_thread_handoff",
    "build_thread_handoff_context_messages",
    "normalize_task_class",
    "plan_session_compaction",
    "recent_raw_messages_for_handoff",
    "resolve_model_context_budget",
    "split_session_messages_for_compaction",
    "thread_handoff_message",
    "validate_context_pack",
]
