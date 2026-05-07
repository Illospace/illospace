"""Thread-local execution context for live AgentRun tool calls."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field, fields
import threading
from typing import Iterator, Mapping


_agent_context = threading.local()


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
            attrs.update(context.to_threadlocal_attrs())
        else:
            attrs.update(dict(context))
    attrs.update(overrides)

    sentinel = object()
    previous = {
        key: getattr(_agent_context, key, sentinel)
        for key in attrs
    }
    try:
        for key, value in attrs.items():
            setattr(_agent_context, key, value)
        yield _agent_context
    finally:
        for key, value in previous.items():
            if value is sentinel:
                if hasattr(_agent_context, key):
                    delattr(_agent_context, key)
            else:
                setattr(_agent_context, key, value)


__all__ = [
    "AgentExecutionContext",
    "bind_agent_context",
    "current_agent_context",
    "get_agent_context_value",
    "snapshot_agent_context",
]
