"""Context-local execution context for live AgentRun tool calls."""

from __future__ import annotations

from contextlib import contextmanager
import contextvars
from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Callable, Iterator, Mapping, TypeVar, cast

if TYPE_CHECKING:
    from brain.platform.integrations.llm import LLMClient
    from brain.platform.integrations.providers import Provider
    from brain.systems.runs.direct_loop.final_reply_evidence import ToolResultEvidence


_StateT = TypeVar("_StateT")


@dataclass
class _AgentRunState:
    """Mutable namespaces intentionally shared by cloned tool-call contexts."""

    values: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedLLMContext:
    """Resolved client objects shared with run-scoped LLM subcalls."""

    llm: LLMClient
    provider: Provider


def _clone_context_value(value: object) -> object:
    """Clone ordinary mutable containers without copying live runtime objects."""
    if isinstance(value, dict):
        return {key: _clone_context_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clone_context_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_clone_context_value(item) for item in value)
    if isinstance(value, set):
        return {_clone_context_value(item) for item in value}
    return value


def clone_agent_context_mapping(mapping: Mapping[str, object]) -> dict[str, object]:
    return {key: _clone_context_value(value) for key, value in dict(mapping).items()}


_context_values: contextvars.ContextVar[dict[str, object]] = contextvars.ContextVar(
    "agent_execution_context",
    default={},
)


class _AgentContextProxy:
    """Context-local attribute proxy for live AgentRun tool calls.

    Agent runs execute tool calls concurrently in the same event-loop thread. A
    plain ``threading.local`` lets parallel async tasks overwrite each other's
    workspace/user context, which can make workspace-bound tools believe they
    are outside a workspace. ``contextvars`` gives every task its own view
    while preserving the attribute-style API the tool handlers use.
    """

    @property
    def __dict__(self) -> dict[str, object]:
        return _context_values.get()

    def __getattr__(self, name: str):
        values = _context_values.get()
        if name in values:
            return values[name]
        raise AttributeError(name)

    def __setattr__(self, name: str, value: object) -> None:
        values = dict(_context_values.get())
        values[name] = value
        _context_values.set(values)

    def __delattr__(self, name: str) -> None:
        values = dict(_context_values.get())
        if name not in values:
            raise AttributeError(name)
        del values[name]
        _context_values.set(values)

    def _copy(self) -> dict[str, object]:
        return clone_agent_context_mapping(_context_values.get())


_agent_context = _AgentContextProxy()


@dataclass
class AgentExecutionContext:
    """Payload exposed to agent tools while an AgentRun recipe is executing."""

    run: object | None = None
    idea_id: str | None = None
    workspace_root: str | None = None
    allowed_workspaces: list[str | dict] = field(default_factory=list)
    user_id: str | None = None
    org_id: str | None = None
    user_request: str | None = None
    worker_name: str | None = None
    session_id: str | None = None
    start_time: float | None = None
    reply_contents: list[str] = field(default_factory=list)
    tool_calls_log: list[str] = field(default_factory=list)
    recent_tool_results: list[ToolResultEvidence] = field(default_factory=list)
    execution_artifacts: list[dict] = field(default_factory=list)
    execution_metadata: dict | None = None
    resolved_llm_context: ResolvedLLMContext | None = None
    intent_satisfaction: dict | None = None
    final_reply_review: dict | None = None
    reply_admission_block_count: int = 0
    resource_summary: dict | None = None
    slash_skill_refs: list[str] = field(default_factory=list)

    def to_context_attrs(self) -> dict:
        """Return a shallow attr mapping without deep-copying live run objects."""
        return {field.name: getattr(self, field.name) for field in fields(self)}


def current_agent_context():
    """Return the task-local AgentRun execution context object."""
    return _agent_context


def get_agent_context_value(name: str, default=None):
    """Read a single value from the current task's AgentRun context."""
    return getattr(_agent_context, name, default)


def get_or_create_agent_run_state(namespace: str, factory: Callable[[], _StateT]) -> _StateT:
    """Return state shared by tool calls in one bound agent run, never globally."""

    holder = getattr(_agent_context, "_run_state", None)
    if not isinstance(holder, _AgentRunState):
        return factory()
    if namespace not in holder.values:
        holder.values[namespace] = factory()
    return cast(_StateT, holder.values[namespace])


def snapshot_agent_context() -> dict:
    """Capture the current task's bound AgentRun context attributes."""
    return _agent_context._copy()


@contextmanager
def bind_agent_context(
    context: AgentExecutionContext | Mapping[str, object] | None = None,
    **overrides,
) -> Iterator[object]:
    """Bind AgentRun context attributes for the current task, then restore them."""
    attrs: dict[str, object] = {}
    if context is not None:
        if isinstance(context, AgentExecutionContext):
            attrs.update(context.to_context_attrs())
        else:
            attrs.update(dict(context))
    attrs.update(overrides)

    next_values = clone_agent_context_mapping(_context_values.get())
    next_values.update(clone_agent_context_mapping(attrs))
    # A fresh explicit binding starts a new run-local scope. Runtime tool-call
    # propagation includes the existing holder in ``attrs`` and therefore
    # deliberately shares it with nested/cloned contexts.
    if not isinstance(attrs.get("_run_state"), _AgentRunState):
        next_values["_run_state"] = _AgentRunState()
    token = _context_values.set(next_values)
    try:
        yield _agent_context
    finally:
        _context_values.reset(token)


__all__ = [
    "AgentExecutionContext",
    "ResolvedLLMContext",
    "bind_agent_context",
    "clone_agent_context_mapping",
    "current_agent_context",
    "get_agent_context_value",
    "get_or_create_agent_run_state",
    "snapshot_agent_context",
]
