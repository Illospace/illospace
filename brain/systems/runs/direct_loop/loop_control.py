"""Loop-control policy for the agent runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import logging
import os
from typing import TYPE_CHECKING

from brain.systems.runs.tool_outcomes import DEFAULT_TOOL_FAILURE_CATEGORY

if TYPE_CHECKING:
    from brain.systems.runs.direct_loop.tool_execution import ResolvedToolCall

logger = logging.getLogger("agent")

_STUCK_WARN_THRESHOLD = 3
_STUCK_BREAK_THRESHOLD = 5
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


class LoopTerminationReason(str, Enum):
    """Named reasons the canonical loop-control policy can stop a run."""

    STUCK_LOOP = "stuck_loop"


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


@dataclass(frozen=True)
class LoopTermination:
    """Typed explanation of why the agent loop must stop."""

    reason: LoopTerminationReason
    message: str
    final_output: str | None = None

    def transcript_message(self) -> dict | None:
        """Return model-authored-looking output only when termination owns it."""

        if self.final_output is None:
            return None
        return {
            "role": "assistant",
            "content": [{"type": "text", "text": self.final_output}],
        }

    def skipped_tool_result(self, block_id: str) -> dict:
        """Pair an unstarted provider tool call after termination has latched."""

        return {
            "type": "tool_result",
            "tool_use_id": block_id,
            "content": self.final_output or self.message,
            "is_error": True,
        }


@dataclass
class LoopControlPolicy:
    """Single owner for stuck-loop termination and per-tool failure control.

    Disabled tools stay latched for this invocation. A newly constructed policy
    is the explicit reset boundary for a subsequent invocation.
    """

    failure_threshold: int = field(default_factory=tool_failure_threshold)
    failure_window_calls: int = field(default_factory=tool_failure_window_calls)
    zero_success_failure_threshold: int = field(
        default_factory=tool_zero_success_failure_threshold
    )
    termination: LoopTermination | None = None
    disabled_tools: dict[str, ToolDisablement] = field(default_factory=dict)
    _tool_failure_windows: dict[str, _ToolFailureWindow] = field(
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

    def detect_stuck_loop(
        self,
        recent_calls: list[str],
        session_id: str,
        messages: list,
    ) -> LoopTermination | None:
        if self.termination is None:
            self.termination = _detect_stuck_loop(recent_calls, session_id, messages)
        return self.termination

    def parallel_same_tool_limit(self, tool_name: str) -> int:
        """Bound speculative duplicates to the nearest failure-control edge."""

        state = self._tool_failure_windows.get(tool_name)
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

    def observe_tool_result(self, resolved: ResolvedToolCall) -> ToolDisablement | None:
        """Observe one resolved call and emit a one-shot tool-disable edge."""

        if self.termination is not None:
            return None
        tool_name = resolved.tool_name or "unknown_tool"
        state = self._tool_failure_windows.setdefault(
            tool_name,
            _ToolFailureWindow(),
        )
        failure = resolved.outcome.failure
        if failure is None:
            state.observe(None, window_calls=self.failure_window_calls)
            return None

        error_class = failure.category or DEFAULT_TOOL_FAILURE_CATEGORY
        state.observe(error_class, window_calls=self.failure_window_calls)
        triggers = self._evaluate_failure_triggers(state, error_class)
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

    def _evaluate_failure_triggers(
        self,
        state: _ToolFailureWindow,
        error_class: str,
    ) -> tuple[ToolFailureTrigger, ...]:
        """Evaluate every tool-failure trigger once for the current outcome."""

        triggers: list[ToolFailureTrigger] = []
        if state.rolling_failures(error_class) >= self.failure_threshold:
            triggers.append(ToolFailureTrigger.ROLLING_WINDOW)
        if (
            state.total_successes == 0
            and state.total_failures >= self.zero_success_failure_threshold
        ):
            triggers.append(ToolFailureTrigger.ZERO_SUCCESS)
        return tuple(triggers)

def _detect_stuck_loop(
    recent_calls: list[str],
    session_id: str,
    messages: list,
    *,
    warn_threshold: int = _STUCK_WARN_THRESHOLD,
    break_threshold: int = _STUCK_BREAK_THRESHOLD,
) -> LoopTermination | None:
    """Return typed termination when identical tool calls prove the loop is stuck."""

    if len(recent_calls) < warn_threshold:
        return None
    tail = recent_calls[-warn_threshold:]
    if len(set(tail)) != 1:
        return None
    repeat_count = sum(1 for fp in reversed(recent_calls) if fp == tail[0])
    if repeat_count >= break_threshold:
        logger.warning("Agent %s: stuck loop (%d identical calls). Breaking.", session_id, repeat_count)
        message = "[System: Agent terminated: stuck in a loop repeating the same tool call]"
        messages.append({"role": "user", "content": [
            {"type": "text", "text": message},
        ]})
        return LoopTermination(
            reason=LoopTerminationReason.STUCK_LOOP,
            message=message,
        )
    return None


def resolve_loop_output(
    termination: LoopTermination | None,
    extracted_output: str,
    staged_reply_contents: list[str],
) -> str:
    """Apply final-output precedence once for every loop termination reason."""

    if termination is not None and termination.final_output is not None:
        return termination.final_output
    if staged_reply_contents:
        return staged_reply_contents[-1]
    return extracted_output


def _inject_nudges(
    recent_calls: list[str],
    *,
    warn_threshold: int = _STUCK_WARN_THRESHOLD,
) -> dict | None:
    """Return a standalone reminder message, or None if nothing to say.

    Strategy is the model's job — the harness does not second-guess it (no
    "try a different strategy" / "consolidate into a script" / "is this reply
    adding new information" nudges). The only thing worth surfacing here is a
    durable, non-obvious mechanical fact that trips models up repeatedly.
    The caller appends the returned message AFTER the tool_results message —
    it must never be spliced into the tool_results content array.
    """

    if (
        len(recent_calls) >= warn_threshold
        and len(set(recent_calls[-warn_threshold:])) == 1
    ):
        return {
            "role": "user",
            "content": (
                "[System: NOTE: `cd` does not persist between exec_command calls; "
                "use `working_dir` or absolute paths.]"
            ),
        }
    return None
