"""Structured execution evidence for deterministic final-reply policies."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from brain.systems.runs.direct_loop.tool_failure_policy import ToolDisablement
from brain.systems.runs.status import RunStatus, coerce_run_status


DEFAULT_TOOL_FAILURE_THRESHOLD = 3


def _structured_value(value: Any) -> Any:
    """Decode a handler's original JSON result without consulting prompt text."""

    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "[{\"":
        return value
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return value


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _searchable_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, Mapping):
        return " ".join(
            part
            for key, item in value.items()
            for part in (_searchable_text(key), _searchable_text(item))
            if part
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return " ".join(filter(None, (_searchable_text(item) for item in value)))
    return str(value)


@dataclass(frozen=True)
class ToolResultEvidence:
    """One tool attempt captured before prompt rendering or truncation."""

    tool_name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    is_error: bool = False
    result: Any = None

    @classmethod
    def capture(
        cls,
        *,
        tool_name: str,
        arguments: Mapping[str, Any] | None,
        is_error: bool,
        result: Any,
    ) -> "ToolResultEvidence":
        return cls(
            tool_name=str(tool_name or "").strip(),
            arguments=_mapping(arguments),
            is_error=bool(is_error),
            result=_structured_value(result),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ToolResultEvidence":
        """Accept structured compatibility payloads, never rendered previews."""

        return cls.capture(
            tool_name=str(value.get("tool_name") or ""),
            arguments=value.get("arguments") if isinstance(value.get("arguments"), Mapping) else {},
            is_error=bool(value.get("is_error")),
            result=value.get("result"),
        )

    @property
    def failed(self) -> bool:
        if self.is_error:
            return True
        return isinstance(self.result, Mapping) and bool(self.result.get("error"))

    @property
    def succeeded(self) -> bool:
        return not self.failed

    def cache_payload(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "is_error": self.is_error,
            "result": self.result,
        }


@dataclass(frozen=True)
class ToolFailureStateEvidence:
    """Read-only projection of tool disablements emitted by the failure guard."""

    failure_threshold: int = DEFAULT_TOOL_FAILURE_THRESHOLD
    total_failures: int = 0
    tool_name: str | None = None
    error_class: str | None = None
    disabled_tools: tuple[str, ...] = ()

    @classmethod
    def from_value(cls, value: Any) -> "ToolFailureStateEvidence | None":
        if value is None:
            return None
        if isinstance(value, Mapping):
            return cls(
                failure_threshold=_coerce_positive_int(
                    value.get("failure_threshold"),
                    default=DEFAULT_TOOL_FAILURE_THRESHOLD,
                ),
                total_failures=_coerce_nonnegative_int(value.get("total_failures")),
                tool_name=(
                    str(value.get("tool_name")).strip()
                    if value.get("tool_name")
                    else None
                ),
                error_class=(
                    str(value.get("error_class")).strip()
                    if value.get("error_class")
                    else None
                ),
                disabled_tools=_coerce_tool_names(value.get("disabled_tools")),
            )
        raw_disabled_tools = getattr(value, "disabled_tools", None)
        disablements = (
            (value,)
            if isinstance(value, ToolDisablement)
            else tuple(
                item
                for item in (
                    raw_disabled_tools.values()
                    if isinstance(raw_disabled_tools, Mapping)
                    else ()
                )
                if isinstance(item, ToolDisablement)
            )
        )
        latest = disablements[-1] if disablements else None
        return cls(
            failure_threshold=_coerce_positive_int(
                getattr(value, "failure_threshold", None),
                default=DEFAULT_TOOL_FAILURE_THRESHOLD,
            ),
            total_failures=_coerce_nonnegative_int(
                latest.total_failures if latest is not None else None
            ),
            tool_name=latest.tool_name if latest is not None else None,
            error_class=latest.error_class if latest is not None else None,
            disabled_tools=tuple(
                disablement.tool_name for disablement in disablements
            ),
        )

    @property
    def guard_triggered(self) -> bool:
        return bool(self.disabled_tools)

    @property
    def threshold_reached(self) -> bool:
        return self.guard_triggered

    def cache_payload(self) -> dict[str, Any]:
        return {
            "failure_threshold": self.failure_threshold,
            "total_failures": self.total_failures,
            "tool_name": self.tool_name,
            "error_class": self.error_class,
            "disabled_tools": list(self.disabled_tools),
        }


@dataclass(frozen=True)
class StatusRunEvidence:
    """One prior same-thread run relevant to a status question."""

    run_id: int | None = None
    status: RunStatus = RunStatus.FAILED
    request: str = ""
    final_output: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "StatusRunEvidence":
        return cls(
            run_id=_coerce_optional_int(value.get("run_id")),
            status=coerce_run_status(
                value.get("status"),
                default=RunStatus.FAILED,
            ),
            request=str(value.get("request") or ""),
            final_output=(
                str(value.get("final_output")).strip()
                if value.get("final_output")
                else None
            ),
        )

    def cache_payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "request": self.request,
            "final_output": self.final_output,
        }


@dataclass(frozen=True)
class StatusDeliverableEvidence:
    """A deliverable that a status reply must account for individually."""

    kind: str
    label: str

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
    ) -> "StatusDeliverableEvidence | None":
        kind = str(value.get("kind") or "").strip().lower()
        label = str(value.get("label") or kind).strip()
        return cls(kind=kind, label=label) if kind and label else None

    def cache_payload(self) -> dict[str, str]:
        return {"kind": self.kind, "label": self.label}


@dataclass(frozen=True)
class StatusQuestionEvidence:
    """Typed snapshot of the originating and live same-thread runs."""

    thread_id: str = ""
    lookup_status: str = ""
    lookup_error: str | None = None
    originating_run: StatusRunEvidence | None = None
    live_sibling_runs: tuple[StatusRunEvidence, ...] = ()
    deliverables: tuple[StatusDeliverableEvidence, ...] = ()

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "StatusQuestionEvidence":
        origin = value.get("originating_run")
        live_runs = tuple(
            StatusRunEvidence.from_mapping(item)
            for item in list(value.get("live_sibling_runs") or [])
            if isinstance(item, Mapping)
        )
        deliverables: list[StatusDeliverableEvidence] = []
        for item in list(value.get("deliverables") or []):
            if not isinstance(item, Mapping):
                continue
            deliverable = StatusDeliverableEvidence.from_mapping(item)
            if deliverable is not None:
                deliverables.append(deliverable)
        return cls(
            thread_id=str(value.get("thread_id") or "").strip(),
            lookup_status=str(value.get("lookup_status") or "").strip().lower(),
            lookup_error=(
                str(value.get("lookup_error")).strip()
                if value.get("lookup_error")
                else None
            ),
            originating_run=(
                StatusRunEvidence.from_mapping(origin)
                if isinstance(origin, Mapping)
                else None
            ),
            live_sibling_runs=live_runs,
            deliverables=tuple(deliverables),
        )

    @property
    def has_live_sibling(self) -> bool:
        return bool(self.live_sibling_runs)

    def cache_payload(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "lookup_status": self.lookup_status,
            "lookup_error": self.lookup_error,
            "originating_run": (
                self.originating_run.cache_payload()
                if self.originating_run is not None
                else None
            ),
            "live_sibling_runs": [
                item.cache_payload() for item in self.live_sibling_runs
            ],
            "deliverables": [
                item.cache_payload() for item in self.deliverables
            ],
        }


@dataclass(frozen=True)
class FinalReplyEvidence:
    """Typed evidence boundary shared by deterministic final-reply policies."""

    tool_results: tuple[ToolResultEvidence, ...] = ()
    execution_artifacts: tuple[Mapping[str, Any], ...] = ()
    worker_results: tuple[Mapping[str, Any], ...] = ()
    status_question: StatusQuestionEvidence | None = None
    tool_failure_state: ToolFailureStateEvidence | None = None

    @classmethod
    def from_agent_context(cls, agent_context: Any) -> "FinalReplyEvidence":
        tool_results: list[ToolResultEvidence] = []
        for item in list(getattr(agent_context, "recent_tool_results", []) or []):
            if isinstance(item, ToolResultEvidence):
                tool_results.append(item)
            elif isinstance(item, Mapping):
                tool_results.append(ToolResultEvidence.from_mapping(item))

        artifacts = tuple(
            dict(item)
            for item in list(getattr(agent_context, "execution_artifacts", []) or [])
            if isinstance(item, Mapping)
        )

        run = getattr(agent_context, "run", None)
        workers: list[Mapping[str, Any]] = []
        for item in list(getattr(run, "worker_results", []) or []):
            workers.append(
                {
                    "success": bool(getattr(item, "success", False)),
                    "output": getattr(item, "output", None),
                    "evidence": getattr(item, "evidence", None),
                }
            )
        status_question = _status_question_evidence_from_context(agent_context)
        failure_state = _tool_failure_state_from_context(agent_context, tool_results)
        return cls(
            tool_results=tuple(tool_results),
            execution_artifacts=artifacts,
            worker_results=tuple(workers),
            status_question=status_question,
            tool_failure_state=failure_state,
        )

    def results_for(self, tool_name: str) -> tuple[ToolResultEvidence, ...]:
        expected = str(tool_name or "").strip()
        return tuple(item for item in self.tool_results if item.tool_name == expected)

    def supports_terms(self, terms: list[str]) -> bool:
        if not terms:
            return False
        successful_tool_results = [
            item.cache_payload()
            for item in self.tool_results
            if item.succeeded
        ]
        successful_artifacts = [
            item
            for item in self.execution_artifacts
            if str(item.get("status") or "").strip().lower()
            not in {"error", "failed", "failure"}
        ]
        successful_workers = [
            item for item in self.worker_results if bool(item.get("success"))
        ]
        text = _searchable_text(
            {
                "tool_results": successful_tool_results,
                "execution_artifacts": successful_artifacts,
                "worker_results": successful_workers,
            }
        ).lower()
        return all(term in text for term in terms)

    @property
    def failed_tool_names(self) -> tuple[str, ...]:
        names: list[str] = []
        state_name = (
            self.tool_failure_state.tool_name
            if self.tool_failure_state is not None
            else None
        )
        for name in [
            *(
                self.tool_failure_state.disabled_tools
                if self.tool_failure_state is not None
                else ()
            ),
            state_name,
            *(item.tool_name for item in self.tool_results if item.failed),
        ]:
            clean_name = str(name or "").strip()
            if clean_name and clean_name not in names:
                names.append(clean_name)
        return tuple(names)

    @property
    def failure_threshold_reached(self) -> bool:
        if (
            self.tool_failure_state is not None
            and self.tool_failure_state.threshold_reached
        ):
            return True
        return (
            sum(1 for item in self.tool_results if item.failed)
            >= DEFAULT_TOOL_FAILURE_THRESHOLD
        )

    def cache_fingerprint(self) -> str:
        payload = {
            "tool_results": [item.cache_payload() for item in self.tool_results],
            "execution_artifacts": self.execution_artifacts,
            "worker_results": self.worker_results,
            "status_question": (
                self.status_question.cache_payload()
                if self.status_question is not None
                else None
            ),
            "tool_failure_state": (
                self.tool_failure_state.cache_payload()
                if self.tool_failure_state is not None
                else None
            ),
        }
        serialized = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _coerce_optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _coerce_nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _coerce_positive_int(value: Any, *, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return max(1, int(default))


def _coerce_tool_names(value: Any) -> tuple[str, ...]:
    if isinstance(value, Mapping):
        raw_names = list(value)
    elif isinstance(value, str):
        raw_names = [value]
    elif isinstance(value, (list, tuple)):
        raw_names = list(value)
    elif isinstance(value, (set, frozenset)):
        raw_names = sorted(value, key=str)
    else:
        raw_names = []
    names: list[str] = []
    for value in raw_names:
        name = str(value or "").strip()
        if name and name not in names:
            names.append(name)
    return tuple(names)


def _find_status_question_mapping(value: Any, *, depth: int = 0) -> Mapping[str, Any] | None:
    if not isinstance(value, Mapping) or depth > 3:
        return None
    direct = value.get("status_question_context")
    if isinstance(direct, Mapping):
        return direct
    for key in ("execution_provenance", "metadata", "request_metadata"):
        nested = value.get(key)
        found = _find_status_question_mapping(nested, depth=depth + 1)
        if found is not None:
            return found
    return None


def _status_question_evidence_from_context(agent_context: Any) -> StatusQuestionEvidence | None:
    execution_metadata = getattr(agent_context, "execution_metadata", None)
    mapping = _find_status_question_mapping(execution_metadata)
    if mapping is None:
        mapping = _find_status_question_mapping(getattr(agent_context, "metadata", None))
    return StatusQuestionEvidence.from_mapping(mapping) if mapping is not None else None


def _tool_failure_state_from_context(
    agent_context: Any,
    tool_results: list[ToolResultEvidence],
) -> ToolFailureStateEvidence | None:
    candidates = [
        getattr(agent_context, "loop_control", None),
        getattr(getattr(agent_context, "state", None), "loop_control", None),
        getattr(getattr(agent_context, "run", None), "loop_control", None),
        getattr(agent_context, "termination", None),
        getattr(getattr(agent_context, "run", None), "termination", None),
    ]
    for candidate in candidates:
        state = ToolFailureStateEvidence.from_value(candidate)
        if state is not None and state.guard_triggered:
            return state

    failures = [item for item in tool_results if item.failed]
    if len(failures) < DEFAULT_TOOL_FAILURE_THRESHOLD:
        return None
    names = {item.tool_name for item in failures if item.tool_name}
    last_result = failures[-1].result
    error_class = (
        str(last_result.get("error_class")).strip()
        if isinstance(last_result, Mapping) and last_result.get("error_class")
        else None
    )
    return ToolFailureStateEvidence(
        failure_threshold=DEFAULT_TOOL_FAILURE_THRESHOLD,
        total_failures=len(failures),
        tool_name=next(iter(names)) if len(names) == 1 else None,
        error_class=error_class,
        disabled_tools=(),
    )


__all__ = [
    "DEFAULT_TOOL_FAILURE_THRESHOLD",
    "FinalReplyEvidence",
    "StatusDeliverableEvidence",
    "StatusQuestionEvidence",
    "StatusRunEvidence",
    "ToolFailureStateEvidence",
    "ToolResultEvidence",
]
