"""Mutable state for a single agent loop run."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from brain.systems.runs.direct_loop.gates import GateState
from brain.systems.runs.direct_loop.result import TokenAccumulator


@dataclass
class ToolFailureState:
    """Readable failure tally shared by retry policy and final-answer gates."""

    threshold: int = 3
    total_failures: int = 0
    consecutive_failures: int = 0
    consecutive_tool_name: str | None = None
    last_error_class: str | None = None
    last_error_message: str | None = None
    circuit_open: bool = False

    def record(self, resolved: Any) -> None:
        """Record one resolved call in execution order and latch at the threshold."""

        if self.circuit_open:
            return
        if not bool(getattr(resolved, "is_error", False)):
            self.consecutive_failures = 0
            self.consecutive_tool_name = None
            return

        tool_name = str(getattr(resolved, "tool_name", "") or "unknown_tool")
        error_class = str(getattr(resolved, "error_class", "") or "ToolError")
        if (
            tool_name == self.consecutive_tool_name
            and error_class == self.last_error_class
        ):
            self.consecutive_failures += 1
        else:
            self.consecutive_failures = 1
            self.consecutive_tool_name = tool_name

        self.total_failures += 1
        self.last_error_class = error_class
        self.last_error_message = str(getattr(resolved, "result_text", "") or "")
        self.circuit_open = self.consecutive_failures >= self.threshold

    def final_answer(self) -> str:
        tool_name = self.consecutive_tool_name or "unknown_tool"
        error_class = self.last_error_class or "ToolError"
        detail = (self.last_error_message or "No error detail was returned.").strip()
        if len(detail) > 500:
            detail = f"{detail[:497]}..."
        return (
            f"I stopped retrying `{tool_name}` after {self.consecutive_failures} consecutive failures "
            f"with error class `{error_class}`. Last error: {detail} "
            "I could not complete the tool-dependent part of the request in this run."
        )


@dataclass
class AgentLoopState:
    """State that evolves across provider turns in the agent loop."""

    messages: list[dict[str, Any]] = field(default_factory=list)
    gates: GateState = field(default_factory=GateState)
    tokens: TokenAccumulator = field(default_factory=TokenAccumulator)
    recent_calls: list[str] = field(default_factory=list)
    tool_calls_made: list[str] = field(default_factory=list)
    tool_failures: ToolFailureState = field(default_factory=ToolFailureState)
    provider: Any | None = None
    provider_name: str | None = None
    operation_type: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
