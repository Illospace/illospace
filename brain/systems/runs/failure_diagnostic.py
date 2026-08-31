"""Typed persistence and read projection for terminal run diagnostics."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.agent_run import AgentRunEventRow, AgentRunRow
from brain.systems.runs.failures import RunFailureCategory, coerce_failure_category


_DIAGNOSTIC_SCHEMA = "typed_v1"
_EXCEPTION_CLASS_WITHHELD = "exception_class_withheld"


def _module_level_exception_type(
    module_name: object,
    class_name: str,
) -> type[BaseException] | None:
    if not isinstance(module_name, str):
        return None
    module = sys.modules.get(module_name)
    candidate = getattr(module, class_name, None) if module is not None else None
    if not isinstance(candidate, type) or not issubclass(candidate, BaseException):
        return None
    return candidate


_TOOL_EXECUTION_EVENT_TYPES = frozenset(
    {"run.tool_started", "run.tool_completed", "run.tool_failed"}
)


class RunFailureStage(str, Enum):
    """Closed set of terminal failure stages written by run producers."""

    RUNNER_EXECUTION = "runner_execution"
    PROJECT_CONTEXT_MATERIALIZATION = "project_context_materialization"
    RECIPE_EXECUTION = "recipe_execution"
    AGENT_EXECUTION = "agent_execution"
    COMPLETION_VERIFICATION = "completion_verification"
    RUNNER_SETTLEMENT = "runner_settlement"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ClassifiedRunFailure:
    """A failure category resolved against its run context before presentation."""

    category: RunFailureCategory
    stage: RunFailureStage


_PRE_TOOL_PRESERVATION_FAILURE_STAGES = frozenset(
    {
        RunFailureStage.RUNNER_EXECUTION,
        RunFailureStage.PROJECT_CONTEXT_MATERIALIZATION,
        RunFailureStage.RECIPE_EXECUTION,
        RunFailureStage.AGENT_EXECUTION,
    }
)


class DiagnosticValueState(str, Enum):
    """Whether a projected diagnostic value is usable by the caller."""

    KNOWN = "known"
    UNKNOWN = "unknown"
    REDACTED = "redacted"


@dataclass(frozen=True)
class RunFailureDiagnostic:
    """Safe typed projection of one failed run's diagnostic metadata."""

    stage: RunFailureStage
    stage_state: DiagnosticValueState
    exception_class: str | None
    exception_class_state: DiagnosticValueState
    tool_execution_started: bool
    terminal: bool = True
    retry_scheduled: bool = False

    def as_payload(self) -> dict[str, Any]:
        return {
            "stage": self.stage.value,
            "stage_state": self.stage_state.value,
            "exception_class": self.exception_class,
            "exception_class_state": self.exception_class_state.value,
            "tool_execution_started": self.tool_execution_started,
            "terminal": self.terminal,
            "retry_scheduled": self.retry_scheduled,
        }


def failure_diagnostic_metadata(
    *,
    stage: RunFailureStage,
    exception_type: type[BaseException] | None = None,
) -> dict[str, Any]:
    """Encode diagnostics that came from typed run producers."""

    if not isinstance(stage, RunFailureStage):
        raise TypeError("failure stage must be a RunFailureStage")
    if stage is RunFailureStage.UNKNOWN:
        raise ValueError("failure producers cannot write an unknown stage")
    if exception_type is not None and (
        not isinstance(exception_type, type)
        or not issubclass(exception_type, BaseException)
    ):
        raise TypeError("exception type must be a BaseException class")
    exception_is_module_level = exception_type is not None and (
        _module_level_exception_type(
            exception_type.__module__,
            exception_type.__name__,
        )
        is exception_type
    )
    return {
        "diagnostic_schema": _DIAGNOSTIC_SCHEMA,
        "stage": stage.value,
        **(
            {
                "exception_class": exception_type.__name__,
                "exception_module": exception_type.__module__,
            }
            if exception_is_module_level
            else {_EXCEPTION_CLASS_WITHHELD: True}
            if exception_type is not None
            else {}
        ),
    }


def failure_category_for_run_context(
    category: RunFailureCategory | str | None,
    *,
    requires_durable_preservation: bool,
    tool_execution_started: bool,
    failure_stage: RunFailureStage,
) -> RunFailureCategory:
    """Classify an internal pre-tool preservation failure as actionable setup work."""

    resolved = coerce_failure_category(category)
    if (
        resolved is RunFailureCategory.INTERNAL
        and requires_durable_preservation
        and not tool_execution_started
        and failure_stage in _PRE_TOOL_PRESERVATION_FAILURE_STAGES
    ):
        return RunFailureCategory.PRESERVATION_SETUP
    return resolved


async def run_tool_execution_started(
    session: AsyncSession,
    *,
    run_id: int,
) -> bool:
    """Query the canonical event vocabulary for evidence of tool execution."""

    event_types = (
        await session.scalars(
            select(AgentRunEventRow.event_type).where(
                AgentRunEventRow.run_id == int(run_id),
                AgentRunEventRow.event_type.in_(_TOOL_EXECUTION_EVENT_TYPES),
            )
        )
    ).all()
    return any(
        str(event_type or "") in _TOOL_EXECUTION_EVENT_TYPES
        for event_type in event_types
    )


def run_failure_metadata(
    *,
    category: RunFailureCategory,
    stage: RunFailureStage,
    exception_type: type[BaseException] | None = None,
) -> dict[str, Any]:
    """Encode the complete persisted envelope for one failed run."""

    if not isinstance(category, RunFailureCategory):
        raise TypeError("failure category must be a RunFailureCategory")
    return {
        "category": category.value,
        **failure_diagnostic_metadata(
            stage=stage,
            exception_type=exception_type,
        ),
    }


def _stage_projection(
    failure_metadata: Mapping[str, Any],
) -> tuple[RunFailureStage, DiagnosticValueState]:
    if "stage" not in failure_metadata:
        return RunFailureStage.UNKNOWN, DiagnosticValueState.UNKNOWN
    try:
        stage = RunFailureStage(failure_metadata["stage"])
    except (TypeError, ValueError):
        return RunFailureStage.UNKNOWN, DiagnosticValueState.REDACTED
    if stage is RunFailureStage.UNKNOWN:
        return stage, DiagnosticValueState.UNKNOWN
    return stage, DiagnosticValueState.KNOWN


def _exception_class_projection(
    failure_metadata: Mapping[str, Any],
) -> tuple[str | None, DiagnosticValueState]:
    if "exception_class" not in failure_metadata:
        if (
            failure_metadata.get("diagnostic_schema") == _DIAGNOSTIC_SCHEMA
            and failure_metadata.get(_EXCEPTION_CLASS_WITHHELD) is True
        ):
            return None, DiagnosticValueState.REDACTED
        return None, DiagnosticValueState.UNKNOWN
    exception_class = failure_metadata.get("exception_class")
    if (
        failure_metadata.get("diagnostic_schema") != _DIAGNOSTIC_SCHEMA
        or not isinstance(exception_class, str)
        or not exception_class
        or _module_level_exception_type(
            failure_metadata.get("exception_module"),
            exception_class,
        )
        is None
    ):
        return None, DiagnosticValueState.REDACTED
    return exception_class, DiagnosticValueState.KNOWN


async def read_run_failure_diagnostic(
    session: AsyncSession,
    *,
    run: AgentRunRow,
) -> RunFailureDiagnostic | None:
    """Read the canonical diagnostic projection for a failed run."""

    run_status = getattr(run.status, "value", run.status)
    if str(run_status or "") != "failed":
        return None

    tool_execution_started = await run_tool_execution_started(
        session,
        run_id=int(run.id),
    )
    metadata = run.metadata_ if isinstance(run.metadata_, Mapping) else {}
    stored_failure = metadata.get("failure")
    failure_metadata = stored_failure if isinstance(stored_failure, Mapping) else {}
    stage, stage_state = _stage_projection(failure_metadata)
    exception_class, exception_class_state = _exception_class_projection(
        failure_metadata
    )
    return RunFailureDiagnostic(
        stage=stage,
        stage_state=stage_state,
        exception_class=exception_class,
        exception_class_state=exception_class_state,
        tool_execution_started=tool_execution_started,
        # Replacement retries have their own current run. A still-failed run
        # is terminal and has no retry scheduled on this run identity.
        retry_scheduled=False,
    )


__all__ = [
    "ClassifiedRunFailure",
    "DiagnosticValueState",
    "RunFailureDiagnostic",
    "RunFailureStage",
    "failure_category_for_run_context",
    "failure_diagnostic_metadata",
    "read_run_failure_diagnostic",
    "run_tool_execution_started",
    "run_failure_metadata",
]
