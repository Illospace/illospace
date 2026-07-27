"""Per-tool failure control for one agent run."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import os
from typing import TYPE_CHECKING

from brain.systems.runs.tool_outcomes import DEFAULT_TOOL_FAILURE_CATEGORY

if TYPE_CHECKING:
    from brain.systems.runs.direct_loop.tool_execution import ResolvedToolCall


TOOL_FAILURE_THRESHOLD_DEFAULT = 3
TOOL_FAILURE_WINDOW_CALLS_DEFAULT = 10
TOOL_ZERO_SUCCESS_FAILURE_THRESHOLD_DEFAULT = 2


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def tool_failure_threshold() -> int:
    """Return failures of one class required inside the rolling call window."""

    return max(
        1,
        _env_int("AGENT_TOOL_FAILURE_THRESHOLD", TOOL_FAILURE_THRESHOLD_DEFAULT),
    )


def tool_failure_window_calls() -> int:
    """Return the number of calls to one tool retained for failure evaluation."""

    return max(
        1,
        _env_int(
            "AGENT_TOOL_FAILURE_WINDOW_CALLS",
            TOOL_FAILURE_WINDOW_CALLS_DEFAULT,
        ),
    )


def tool_zero_success_failure_threshold() -> int:
    """Return failures allowed before disabling a tool that has never succeeded."""

    return max(
        2,
        _env_int(
            "AGENT_TOOL_ZERO_SUCCESS_FAILURE_THRESHOLD",
            TOOL_ZERO_SUCCESS_FAILURE_THRESHOLD_DEFAULT,
        ),
    )


class ToolFailureTrigger(str, Enum):
    """Edges evaluated together when deciding whether to disable one tool."""

    ROLLING_WINDOW = "rolling_window"
    ZERO_SUCCESS = "zero_success"


@dataclass
class _ToolFailureWindow:
    """Per-tool outcomes retained only for the current agent invocation."""

    recent_error_classes: list[str | None] = field(default_factory=list)
    total_failures: int = 0
    total_successes: int = 0

    def observe(self, error_class: str | None, *, window_calls: int) -> None:
        self.recent_error_classes.append(error_class)
        if len(self.recent_error_classes) > window_calls:
            del self.recent_error_classes[:-window_calls]
        if error_class is None:
            self.total_successes += 1
        else:
            self.total_failures += 1

    def rolling_failures(self, error_class: str) -> int:
        return self.recent_error_classes.count(error_class)

    def largest_rolling_failure_count(self) -> int:
        error_classes = {
            error_class
            for error_class in self.recent_error_classes
            if error_class is not None
        }
        return max(
            (self.rolling_failures(error_class) for error_class in error_classes),
            default=0,
        )


@dataclass(frozen=True)
class ToolDisablement:
    """One-shot record explaining why a tool left this run's surface."""

    tool_name: str
    error_class: str
    triggers: tuple[ToolFailureTrigger, ...]
    rolling_failures: int
    total_failures: int
    total_successes: int
    window_calls: int
    last_error_message: str

    def model_note(self) -> str:
        trigger_details: list[str] = []
        if ToolFailureTrigger.ROLLING_WINDOW in self.triggers:
            trigger_details.append(
                f"{self.rolling_failures} `{self.error_class}` failures in the "
                f"last {self.window_calls} calls to this tool"
            )
        if ToolFailureTrigger.ZERO_SUCCESS in self.triggers:
            trigger_details.append(
                f"{self.total_failures} failures with zero successes"
            )
        trigger_summary = "; ".join(trigger_details)
        detail = (
            self.last_error_message or "No error detail was returned."
        ).strip()
        if len(detail) > 500:
            detail = f"{detail[:497]}..."
        return (
            f"[System: Tool `{self.tool_name}` is unavailable for the rest of this run; "
            "proceed degraded and record the gap. "
            f"Guard trigger: {trigger_summary}. Observed {self.total_failures} failures "
            f"and {self.total_successes} successes for this tool. "
            f"Last error (`{self.error_class}`): {detail}]"
        )

    def skipped_tool_result(self, block_id: str) -> dict:
        """Pair an unstarted call to this disabled tool without stopping its batch."""

        return {
            "type": "tool_result",
            "tool_use_id": block_id,
            "content": self.model_note(),
            "is_error": True,
        }


@dataclass
class ToolFailurePolicy:
    """Own failure windows and tool-disablement latching."""

    failure_threshold: int = field(default_factory=tool_failure_threshold)
    failure_window_calls: int = field(default_factory=tool_failure_window_calls)
    zero_success_failure_threshold: int = field(
        default_factory=tool_zero_success_failure_threshold
    )
    disabled_tools: dict[str, ToolDisablement] = field(default_factory=dict)
    _windows: dict[str, _ToolFailureWindow] = field(
        default_factory=dict,
        repr=False,
    )

    def __post_init__(self) -> None:
        self.failure_threshold = max(1, int(self.failure_threshold))
        self.failure_window_calls = max(
            self.failure_threshold,
            int(self.failure_window_calls),
        )
        self.zero_success_failure_threshold = max(
            2,
            int(self.zero_success_failure_threshold),
        )

    def parallel_same_tool_limit(self, tool_name: str) -> int:
        """Bound speculative duplicates to the nearest failure-control edge."""

        state = self._windows.get(tool_name)
        rolling_failures = (
            state.largest_rolling_failure_count() if state is not None else 0
        )
        remaining = [self.failure_threshold - rolling_failures]
        if state is None or state.total_successes == 0:
            remaining.append(
                self.zero_success_failure_threshold
                - (state.total_failures if state is not None else 0)
            )
        return max(1, min(remaining))

    def observe_tool_result(
        self,
        resolved: ResolvedToolCall,
    ) -> ToolDisablement | None:
        """Observe one resolved call and emit a one-shot tool-disable edge."""

        tool_name = resolved.tool_name or "unknown_tool"
        state = self._windows.setdefault(tool_name, _ToolFailureWindow())
        failure = resolved.outcome.failure
        if failure is None:
            state.observe(None, window_calls=self.failure_window_calls)
            return None

        error_class = failure.category or DEFAULT_TOOL_FAILURE_CATEGORY
        state.observe(error_class, window_calls=self.failure_window_calls)
        triggers = self._evaluate_triggers(state, error_class)
        if not triggers or tool_name in self.disabled_tools:
            return None

        disablement = ToolDisablement(
            tool_name=tool_name,
            error_class=error_class,
            triggers=triggers,
            rolling_failures=state.rolling_failures(error_class),
            total_failures=state.total_failures,
            total_successes=state.total_successes,
            window_calls=self.failure_window_calls,
            last_error_message=resolved.result_text or "",
        )
        self.disabled_tools[tool_name] = disablement
        return disablement

    def _evaluate_triggers(
        self,
        state: _ToolFailureWindow,
        error_class: str,
    ) -> tuple[ToolFailureTrigger, ...]:
        triggers: list[ToolFailureTrigger] = []
        if state.rolling_failures(error_class) >= self.failure_threshold:
            triggers.append(ToolFailureTrigger.ROLLING_WINDOW)
        if (
            state.total_successes == 0
            and state.total_failures >= self.zero_success_failure_threshold
        ):
            triggers.append(ToolFailureTrigger.ZERO_SUCCESS)
        return tuple(triggers)
