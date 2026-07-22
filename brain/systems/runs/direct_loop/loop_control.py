"""Loop-control policy for the agent runtime."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from brain.systems.runs.direct_loop.tool_execution import ResolvedToolCall

logger = logging.getLogger("agent")

_STUCK_WARN_THRESHOLD = 3
_STUCK_BREAK_THRESHOLD = 5


class LoopTerminationReason(str, Enum):
    """Named reasons the canonical loop-control policy can stop a run."""

    STUCK_LOOP = "stuck_loop"
    TOOL_FAILURE_CIRCUIT = "tool_failure_circuit"


@dataclass(frozen=True)
class LoopTermination:
    """Typed explanation of why the agent loop must stop."""

    reason: LoopTerminationReason
    message: str
    final_output: str | None = None
    tool_name: str | None = None
    error_class: str | None = None
    consecutive_failures: int = 0
    total_failures: int = 0

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
    """Single owner for stuck-loop and repeated-tool-failure termination."""

    failure_threshold: int = 3
    total_failures: int = 0
    consecutive_failures: int = 0
    consecutive_tool_name: str | None = None
    last_error_class: str | None = None
    last_error_message: str | None = None
    termination: LoopTermination | None = None

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
        """Bound speculative duplicates to the remaining failure allowance."""

        threshold = max(1, int(self.failure_threshold))
        if self.consecutive_tool_name != tool_name:
            return threshold
        return max(1, threshold - self.consecutive_failures)

    def observe_tool_result(self, resolved: ResolvedToolCall) -> LoopTermination | None:
        """Observe one resolved call in execution order and latch termination."""

        if self.termination is not None:
            return self.termination
        if not resolved.is_error:
            self.consecutive_failures = 0
            self.consecutive_tool_name = None
            return None

        tool_name = resolved.tool_name or "unknown_tool"
        error_class = resolved.error_class or "ToolError"
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
        self.last_error_message = resolved.result_text or ""
        if self.consecutive_failures >= max(1, int(self.failure_threshold)):
            self.termination = self._failure_circuit_termination()
        return self.termination

    def _failure_circuit_termination(self) -> LoopTermination:
        tool_name = self.consecutive_tool_name or "unknown_tool"
        error_class = self.last_error_class or "ToolError"
        detail = (self.last_error_message or "No error detail was returned.").strip()
        if len(detail) > 500:
            detail = f"{detail[:497]}..."
        output = (
            f"I stopped retrying `{tool_name}` after {self.consecutive_failures} consecutive failures "
            f"with error class `{error_class}`. Last error: {detail} "
            "I could not complete the tool-dependent part of the request in this run."
        )
        return LoopTermination(
            reason=LoopTerminationReason.TOOL_FAILURE_CIRCUIT,
            message=output,
            final_output=output,
            tool_name=tool_name,
            error_class=error_class,
            consecutive_failures=self.consecutive_failures,
            total_failures=self.total_failures,
        )


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
