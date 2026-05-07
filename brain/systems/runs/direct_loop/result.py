"""Agent result and token accounting helpers."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class _TokenAccumulator:
    """Accumulates token usage across provider turns."""

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_creation: int = 0

    def add_turn(self, usage):
        self.input += getattr(usage, "input_tokens", 0)
        self.output += getattr(usage, "output_tokens", 0)
        self.cache_read += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.cache_creation += getattr(usage, "cache_creation_input_tokens", 0) or 0


TokenAccumulator = _TokenAccumulator


@dataclass
class AgentResult:
    """Result from an agent run."""

    output: str
    success: bool
    session_id: str
    tokens_input: int = 0
    tokens_output: int = 0
    tokens_cache_read: int = 0
    tokens_cache_creation: int = 0
    duration_sec: int = 0
    tool_calls: list[str] = field(default_factory=list)
    worker_results: list = field(default_factory=list)
    error: str | None = None


def make_result(
    output: str,
    success: bool,
    session_id: str,
    tokens: _TokenAccumulator,
    start_time: float,
    tool_calls: list[str],
    error: str | None = None,
    worker_results: list | None = None,
) -> AgentResult:
    """Construct an AgentResult with common runtime fields."""

    return AgentResult(
        output=output,
        success=success,
        session_id=session_id,
        tokens_input=tokens.input,
        tokens_output=tokens.output,
        tokens_cache_read=tokens.cache_read,
        tokens_cache_creation=tokens.cache_creation,
        duration_sec=int(time.time() - start_time),
        tool_calls=tool_calls,
        error=error,
        worker_results=worker_results or [],
    )
