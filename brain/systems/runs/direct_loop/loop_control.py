"""Run-control façade for tool failures and stuck-loop termination."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import logging
from typing import TYPE_CHECKING

from brain.systems.runs.direct_loop.loop_guard import (
    LoopGuard,
    LoopSignal,
    LoopTrigger,
)
from brain.systems.runs.direct_loop.tool_failure_policy import (
    ToolDisablement,
    ToolFailurePolicy,
    tool_failure_threshold,
    tool_failure_window_calls,
    tool_zero_success_failure_threshold,
)

if TYPE_CHECKING:
    from brain.systems.runs.direct_loop.tool_execution import ResolvedToolCall

logger = logging.getLogger("agent")


class LoopTerminationReason(str, Enum):
    """Named reasons run control can stop an agent."""

    STUCK_LOOP = "stuck_loop"


@dataclass(frozen=True)
class LoopTermination:
    """Typed explanation of why the agent loop must stop."""

    reason: LoopTerminationReason
    message: str
    trigger: LoopTrigger
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

    def control_message(self) -> dict:
        """Return a system-authored transcript notice for policy termination."""

        return {
            "role": "user",
            "content": [{"type": "text", "text": self.message}],
        }


@dataclass(frozen=True)
class ControlDecision:
    """One resolved call's complete run-control outcome."""

    termination: LoopTermination | None = None
    disablement: ToolDisablement | None = None


@dataclass
class ControlEvaluator:
    """Own loop-signal precedence and termination latching."""

    session_id: str = "unknown"
    termination: LoopTermination | None = None

    _SIGNAL_PRECEDENCE = {
        LoopTrigger.UNCHANGED_RESULT: 0,
        LoopTrigger.EXACT_REPEAT: 1,
        LoopTrigger.SEMANTIC_NO_PROGRESS: 2,
    }

    def evaluate(
        self,
        loop_signals: tuple[LoopSignal, ...],
        disablement: ToolDisablement | None,
    ) -> ControlDecision:
        if self.termination is not None:
            return ControlDecision(termination=self.termination)
        if loop_signals:
            signal = min(
                loop_signals,
                key=lambda item: self._SIGNAL_PRECEDENCE[item.trigger],
            )
            self.termination = LoopTermination(
                reason=LoopTerminationReason.STUCK_LOOP,
                message=signal.message,
                trigger=signal.trigger,
            )
            logger.warning(
                "Agent %s: %s",
                self.session_id,
                signal.message.removeprefix("[System: ").removesuffix("]"),
            )
            return ControlDecision(termination=self.termination)
        return ControlDecision(disablement=disablement)


class RunControlPolicy:
    """Compose loop protection and per-tool failure control for one run."""

    def __init__(
        self,
        *,
        failure_threshold: int | None = None,
        failure_window_calls: int | None = None,
        zero_success_failure_threshold: int | None = None,
        semantic_stall_threshold: int = 10,
        unchanged_result_threshold: int = 4,
        exact_repeat_threshold: int = 5,
        session_id: str = "unknown",
    ) -> None:
        self.tool_failures = ToolFailurePolicy(
            failure_threshold=(
                tool_failure_threshold()
                if failure_threshold is None
                else failure_threshold
            ),
            failure_window_calls=(
                tool_failure_window_calls()
                if failure_window_calls is None
                else failure_window_calls
            ),
            zero_success_failure_threshold=(
                tool_zero_success_failure_threshold()
                if zero_success_failure_threshold is None
                else zero_success_failure_threshold
            ),
        )
        self.loop_guard = LoopGuard(
            semantic_stall_threshold=semantic_stall_threshold,
            unchanged_result_threshold=unchanged_result_threshold,
            exact_repeat_threshold=exact_repeat_threshold,
        )
        self.evaluator = ControlEvaluator(session_id=session_id)

    @property
    def failure_threshold(self) -> int:
        return self.tool_failures.failure_threshold

    @property
    def failure_window_calls(self) -> int:
        return self.tool_failures.failure_window_calls

    @property
    def zero_success_failure_threshold(self) -> int:
        return self.tool_failures.zero_success_failure_threshold

    @property
    def termination(self) -> LoopTermination | None:
        return self.evaluator.termination

    @property
    def disabled_tools(self) -> dict[str, ToolDisablement]:
        return self.tool_failures.disabled_tools

    def parallel_same_tool_limit(self, tool_name: str) -> int:
        return self.tool_failures.parallel_same_tool_limit(tool_name)

    def observe_tool_result(self, resolved: ResolvedToolCall) -> ControlDecision:
        """Evaluate every control policy and return one authoritative decision."""

        loop_signals = self.loop_guard.observe_tool_result(resolved)
        disablement = self.tool_failures.observe_tool_result(resolved)
        return self.evaluator.evaluate(loop_signals, disablement)

    def reminder_message(self) -> dict | None:
        """Return a durable runtime reminder for a repeated exact call."""

        return self.loop_guard.reminder_message()


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
