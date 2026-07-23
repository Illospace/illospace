"""Typed outcome contracts shared by tool handlers and execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


DEFAULT_TOOL_FAILURE_CATEGORY = "ToolError"


@dataclass(frozen=True)
class ToolFailure:
    """Stable failure identity shared by tool handlers and loop policy."""

    message: str
    category: str


@dataclass(frozen=True)
class ToolOutcome:
    """Typed answer to whether a tool result failed and how."""

    failure: ToolFailure | None = None

    @classmethod
    def failed(cls, *, message: str, category: str) -> ToolOutcome:
        return cls(failure=ToolFailure(message=message, category=category))


@dataclass(frozen=True)
class ToolHandlerResult:
    """A handler value paired with its explicit execution outcome."""

    value: Any
    outcome: ToolOutcome
