"""Progress tracking and named stuck-loop detectors."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
from typing import Any, TYPE_CHECKING

from brain.systems.runs.tool_catalog.metadata import ToolSideEffectClass

if TYPE_CHECKING:
    from brain.systems.runs.direct_loop.tool_execution import ResolvedToolCall


_STUCK_WARN_THRESHOLD = 3
_STUCK_BREAK_THRESHOLD = 5
_SEMANTIC_STALL_THRESHOLD = 10
_UNCHANGED_RESULT_THRESHOLD = 4


class Progress(str, Enum):
    """How one result changed the known state of its declared target."""

    NEW_TARGET = "new_target"
    CHANGED = "changed"
    UNCHANGED = "unchanged"


class LoopTrigger(str, Enum):
    """Named detector edges considered by run-control precedence."""

    EXACT_REPEAT = "exact_repeat"
    SEMANTIC_NO_PROGRESS = "semantic_no_progress"
    UNCHANGED_RESULT = "unchanged_result"


@dataclass(frozen=True)
class ToolCallIdentity:
    """Exact invocation identity plus its catalog-declared stable target."""

    exact_key: str
    target_key: str


@dataclass(frozen=True)
class ProgressObservation:
    """The sole observation contract consumed by loop detectors."""

    identity: ToolCallIdentity
    result_key: str
    progress: Progress


@dataclass(frozen=True)
class LoopSignal:
    """A detector edge with provider-facing termination context."""

    trigger: LoopTrigger
    message: str


def _canonical_key(tool_name: str, tool_input: dict[str, Any]) -> str:
    return f"{tool_name}:{json.dumps(tool_input, sort_keys=True)}"


def tool_call_target_key(
    tool_name: str,
    tool_input: dict[str, Any],
) -> str:
    """Project a call to the stable target declared by its tool registration."""

    from brain.systems.runs.tool_catalog.registry import get_tool_registration

    registration = get_tool_registration(tool_name)
    projected_input = (
        registration.identity_spec.project(tool_input)
        if registration is not None and registration.identity_spec is not None
        else dict(tool_input)
    )
    return _canonical_key(tool_name, projected_input)


def tool_call_identity(
    tool_name: str,
    tool_input: dict[str, Any],
) -> ToolCallIdentity:
    """Return both identities needed to evaluate one resolved call."""

    return ToolCallIdentity(
        exact_key=_canonical_key(tool_name, tool_input),
        target_key=tool_call_target_key(tool_name, tool_input),
    )


def tool_result_key(resolved: ResolvedToolCall) -> str:
    """Hash the canonical resolved value used to judge target progress."""

    value = (
        resolved.result_value
        if resolved.result_value is not None
        else resolved.result_text
    )
    encoded = json.dumps(
        value,
        default=str,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def is_loop_progress_candidate(tool_name: str) -> bool:
    """Limit progress heuristics to registry-declared read-only tools."""

    from brain.systems.runs.tool_catalog.registry import get_tool_registration

    registration = get_tool_registration(tool_name)
    return (
        registration is not None
        and registration.side_effect_class
        in {
            ToolSideEffectClass.READ_ONLY,
            ToolSideEffectClass.READ_ONLY_EXTERNAL,
        }
    )


@dataclass
class ProgressTracker:
    """Own the last result for each declared target."""

    _last_results: dict[str, str] = field(default_factory=dict, repr=False)

    def observe(
        self,
        identity: ToolCallIdentity,
        result_key: str,
    ) -> ProgressObservation:
        previous = self._last_results.get(identity.target_key)
        if previous is None:
            progress = Progress.NEW_TARGET
        elif previous != result_key:
            progress = Progress.CHANGED
        else:
            progress = Progress.UNCHANGED
        self._last_results[identity.target_key] = result_key
        return ProgressObservation(
            identity=identity,
            result_key=result_key,
            progress=progress,
        )


@dataclass
class ExactRepeatDetector:
    """Detect identical calls only while their observed results make no progress."""

    break_threshold: int = _STUCK_BREAK_THRESHOLD
    warn_threshold: int = _STUCK_WARN_THRESHOLD
    _last_exact_key: str | None = field(default=None, repr=False)
    _repeat_count: int = field(default=0, repr=False)

    def __post_init__(self) -> None:
        self.break_threshold = max(1, int(self.break_threshold))
        self.warn_threshold = max(1, int(self.warn_threshold))

    def observe(self, observation: ProgressObservation) -> LoopSignal | None:
        if observation.progress is Progress.CHANGED:
            self._last_exact_key = None
            self._repeat_count = 0
            return None
        if observation.identity.exact_key == self._last_exact_key:
            self._repeat_count += 1
        else:
            self._last_exact_key = observation.identity.exact_key
            self._repeat_count = 1
        if self._repeat_count < self.break_threshold:
            return None
        return LoopSignal(
            trigger=LoopTrigger.EXACT_REPEAT,
            message=(
                "[System: Agent terminated: stuck in a loop repeating the same "
                "tool call]"
            ),
        )

    def reminder_message(self) -> dict | None:
        """Return the one durable reminder attached to exact repetition."""

        if self._repeat_count < self.warn_threshold:
            return None
        return {
            "role": "user",
            "content": (
                "[System: NOTE: `cd` does not persist between exec_command calls; "
                "use `working_dir` or absolute paths.]"
            ),
        }


@dataclass
class SemanticNoProgressDetector:
    """Detect repeated known targets whose results stay unchanged."""

    threshold: int = _SEMANTIC_STALL_THRESHOLD
    _streak: int = field(default=0, repr=False)

    def __post_init__(self) -> None:
        self.threshold = max(1, int(self.threshold))

    def observe(self, observation: ProgressObservation) -> LoopSignal | None:
        if observation.progress is Progress.UNCHANGED:
            self._streak += 1
        else:
            self._streak = 0
        if self._streak < self.threshold:
            return None
        return LoopSignal(
            trigger=LoopTrigger.SEMANTIC_NO_PROGRESS,
            message=(
                "[System: Agent terminated: stuck in a semantic tool-call loop "
                "with no new target or result]"
            ),
        )


@dataclass
class UnchangedResultDetector:
    """Detect one target returning the same result too many times."""

    threshold: int = _UNCHANGED_RESULT_THRESHOLD
    _observations_by_target: dict[str, int] = field(
        default_factory=dict,
        repr=False,
    )

    def __post_init__(self) -> None:
        self.threshold = max(2, int(self.threshold))

    def observe(self, observation: ProgressObservation) -> LoopSignal | None:
        target_key = observation.identity.target_key
        if observation.progress is Progress.UNCHANGED:
            count = self._observations_by_target.get(target_key, 1) + 1
        else:
            count = 1
        self._observations_by_target[target_key] = count
        if count < self.threshold:
            return None
        return LoopSignal(
            trigger=LoopTrigger.UNCHANGED_RESULT,
            message=(
                "[System: Agent terminated: stuck in a loop repeating the same "
                "read-only tool call with an unchanged result]"
            ),
        )


@dataclass
class LoopGuard:
    """Compose progress tracking and independent named loop detectors."""

    semantic_stall_threshold: int = _SEMANTIC_STALL_THRESHOLD
    unchanged_result_threshold: int = _UNCHANGED_RESULT_THRESHOLD
    exact_repeat_threshold: int = _STUCK_BREAK_THRESHOLD
    progress_tracker: ProgressTracker = field(default_factory=ProgressTracker)
    exact_repeats: ExactRepeatDetector = field(init=False)
    semantic_no_progress: SemanticNoProgressDetector = field(init=False)
    unchanged_results: UnchangedResultDetector = field(init=False)

    def __post_init__(self) -> None:
        self.exact_repeats = ExactRepeatDetector(
            break_threshold=self.exact_repeat_threshold,
        )
        self.semantic_no_progress = SemanticNoProgressDetector(
            threshold=self.semantic_stall_threshold,
        )
        self.unchanged_results = UnchangedResultDetector(
            threshold=self.unchanged_result_threshold,
        )

    def observe_tool_result(
        self,
        resolved: ResolvedToolCall,
    ) -> tuple[LoopSignal, ...]:
        identity = tool_call_identity(
            resolved.tool_name or "unknown_tool",
            resolved.tool_input,
        )
        observation = self.progress_tracker.observe(
            identity,
            tool_result_key(resolved),
        )
        signals = [self.exact_repeats.observe(observation)]
        if is_loop_progress_candidate(resolved.tool_name or "unknown_tool"):
            signals.extend(
                (
                    self.semantic_no_progress.observe(observation),
                    self.unchanged_results.observe(observation),
                )
            )
        return tuple(signal for signal in signals if signal is not None)

    def reminder_message(self) -> dict | None:
        return self.exact_repeats.reminder_message()
