"""Context-local execution context for live AgentRun tool calls."""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass, field, fields
from typing import Iterator, Mapping


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
    recent_tool_results: list[dict] = field(default_factory=list)
    execution_artifacts: list[dict] = field(default_factory=list)
    execution_metadata: dict | None = None
    intent_satisfaction: dict | None = None
    final_reply_review: dict | None = None
    resource_summary: dict | None = None
    slash_skill_refs: list[str] = field(default_factory=list)

    def to_context_attrs(self) -> dict:
        """Return a shallow attr mapping without deep-copying live run objects."""
        return {field.name: getattr(self, field.name) for field in fields(self)}


def current_agent_context():
    """Return the thread-local AgentRun execution context object."""
    return _agent_context


def get_agent_context_value(name: str, default=None):
    """Read a single value from the current thread's AgentRun context."""
    return getattr(_agent_context, name, default)


def snapshot_agent_context() -> dict:
    """Capture the current thread's bound AgentRun context attributes."""
    return vars(_agent_context).copy()


@contextmanager
def bind_agent_context(
    context: AgentExecutionContext | Mapping[str, object] | None = None,
    **overrides,
) -> Iterator[object]:
    """Bind AgentRun context attributes for the current thread, then restore them."""
    attrs: dict[str, object] = {}
    if context is not None:
        if isinstance(context, AgentExecutionContext):
            attrs.update(context.to_context_attrs())
        else:
            attrs.update(dict(context))
    attrs.update(overrides)

    next_values = dict(_context_values.get())
    next_values.update(attrs)
    token = _context_values.set(next_values)
    try:
        yield _agent_context
    finally:
        _context_values.reset(token)


__all__ = [
    "AgentExecutionContext",
    "bind_agent_context",
    "current_agent_context",
    "get_agent_context_value",
    "snapshot_agent_context",
]
