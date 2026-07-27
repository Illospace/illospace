"""Mutable state for a single agent loop run."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from brain.systems.runs.direct_loop.gates import GateState
from brain.systems.runs.direct_loop.loop_control import LoopControlPolicy
from brain.systems.runs.direct_loop.result import TokenAccumulator


@dataclass
class AgentLoopState:
    """State that evolves across provider turns in the agent loop."""

    messages: list[dict[str, Any]] = field(default_factory=list)
    gates: GateState = field(default_factory=GateState)
    tokens: TokenAccumulator = field(default_factory=TokenAccumulator)
    recent_calls: list[str] = field(default_factory=list)
    recent_semantic_calls: list[str] = field(default_factory=list)
    tool_calls_made: list[str] = field(default_factory=list)
    loop_control: LoopControlPolicy = field(default_factory=LoopControlPolicy)
    provider: Any | None = None
    provider_name: str | None = None
    operation_type: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
