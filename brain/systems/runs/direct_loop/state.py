"""Mutable state for a single agent loop run."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from brain.systems.runs.direct_loop.gates import GateState
from brain.systems.runs.direct_loop.loop_control import RunControlPolicy
from brain.systems.runs.direct_loop.result import TokenAccumulator


@dataclass
class ContextCompactionTracker:
    """Track consecutive compactions that cannot get below the context ceiling."""

    consecutive_no_progress: int = 0
    warned_no_safe_messages: bool = False

    def reset(self) -> None:
        self.consecutive_no_progress = 0
        self.warned_no_safe_messages = False


@dataclass
class AgentLoopState:
    """State that evolves across provider turns in the agent loop."""

    messages: list[dict[str, Any]] = field(default_factory=list)
    gates: GateState = field(default_factory=GateState)
    tokens: TokenAccumulator = field(default_factory=TokenAccumulator)
    tool_calls_made: list[str] = field(default_factory=list)
    loop_control: RunControlPolicy = field(default_factory=RunControlPolicy)
    provider: Any | None = None
    provider_name: str | None = None
    operation_type: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    context_compaction: ContextCompactionTracker = field(default_factory=ContextCompactionTracker)
