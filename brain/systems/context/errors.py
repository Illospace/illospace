"""Typed failures raised by model-context admission and compaction."""

from __future__ import annotations


class ContextFloorExceedsBudgetError(RuntimeError):
    """The smallest prompt produced by canonical compaction cannot be admitted."""

    def __init__(self, *, floor: int, ceiling: int, tools: int, min_messages: int):
        self.floor = int(floor)
        self.ceiling = int(ceiling)
        self.tools = int(tools)
        self.min_messages = int(min_messages)
        super().__init__(
            "context_floor_exceeds_budget: "
            f"floor={self.floor} ceiling={self.ceiling} tools={self.tools} "
            f"min_messages={self.min_messages}"
        )


class ContextCompactionStalledError(RuntimeError):
    """Repeated compaction attempts cannot produce an admissible prompt."""

    def __init__(
        self,
        *,
        estimated: int,
        ceiling: int,
        tools: int,
        attempts: int,
        phase: str,
    ):
        self.estimated = int(estimated)
        self.ceiling = int(ceiling)
        self.tools = int(tools)
        self.attempts = int(attempts)
        self.phase = str(phase)
        super().__init__(
            "context_compaction_stalled: "
            f"estimated={self.estimated} ceiling={self.ceiling} tools={self.tools} "
            f"attempts={self.attempts} phase={self.phase}"
        )


__all__ = [
    "ContextCompactionStalledError",
    "ContextFloorExceedsBudgetError",
]
