"""Mutable state for a single agent loop run."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from brain.systems.runs.direct_loop.gates import GateState
from brain.systems.runs.direct_loop.loop_control import LoopControlPolicy
from brain.systems.runs.direct_loop.result import TokenAccumulator


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

    @property
    def is_failure(self) -> bool:
        return self.failure is not None


class ClassifiedToolResult(str):
    """String-compatible handler result carrying an internal typed outcome."""

    outcome: ToolOutcome

    def __new__(cls, value: str, outcome: ToolOutcome) -> ClassifiedToolResult:
        instance = super().__new__(cls, value)
        instance.outcome = outcome
        return instance


@dataclass
class AgentLoopState:
    """State that evolves across provider turns in the agent loop."""

    messages: list[dict[str, Any]] = field(default_factory=list)
    gates: GateState = field(default_factory=GateState)
    tokens: TokenAccumulator = field(default_factory=TokenAccumulator)
    recent_calls: list[str] = field(default_factory=list)
    tool_calls_made: list[str] = field(default_factory=list)
    loop_control: LoopControlPolicy = field(default_factory=LoopControlPolicy)
    provider: Any | None = None
    provider_name: str | None = None
    operation_type: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
