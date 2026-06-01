"""Task-local execution context for live AgentRun tool calls."""

from __future__ import annotations

from contextlib import contextmanager
import contextvars
from dataclasses import dataclass, field, fields
from typing import Iterator, Mapping


_context_state: contextvars.ContextVar[dict[str, object] | None] = contextvars.ContextVar(
    "illo_agent_execution_context",
    default=None,
)


def _state() -> dict[str, object]:
    state = _context_state.get()
    if state is None:
        state = {}
        _context_state.set(state)
    return state


class _AgentContext:
    """A small namespace proxy backed by ContextVar state.

    Agent tools read and write attributes on this object. ContextVar storage keeps
    concurrent async tool calls from mutating the same logical context while still
    letting worker threads start with their own empty context.
    """

    def __getattribute__(self, name: str):
        if name in {"__class__", "__dict__", "_copy"}:
            if name == "__dict__":
                return _state()
            return object.__getattribute__(self, name)
        try:
            return _state()[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name: str, value: object) -> None:
        _state()[name] = value

    def __delattr__(self, name: str) -> None:
        try:
            del _state()[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def _copy(self) -> dict[str, object]:
        return dict(_state())


_agent_context = _AgentContext()


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
    execution_artifacts: list[dict] = field(default_factory=list)
    execution_metadata: dict | None = None
    intent_satisfaction: dict | None = None
    final_reply_review: dict | None = None
    resource_summary: dict | None = None
    slash_skill_refs: list[str] = field(default_factory=list)

    def to_threadlocal_attrs(self) -> dict:
        """Return a shallow attr mapping without deep-copying live run objects."""
        return {field.name: getattr(self, field.name) for field in fields(self)}


def current_agent_context():
    """Return the task-local AgentRun execution context object."""
    return _agent_context


def get_agent_context_value(name: str, default=None):
    """Read a single value from the current task's AgentRun context."""
    return getattr(_agent_context, name, default)


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
            attrs.update(context.to_threadlocal_attrs())
        else:
            attrs.update(dict(context))
    attrs.update(overrides)

    next_state = _agent_context._copy()
    next_state.update(attrs)
    token = _context_state.set(next_state)
    try:
        yield _agent_context
    finally:
        _context_state.reset(token)


__all__ = [
    "AgentExecutionContext",
    "bind_agent_context",
    "current_agent_context",
    "get_agent_context_value",
    "snapshot_agent_context",
]
