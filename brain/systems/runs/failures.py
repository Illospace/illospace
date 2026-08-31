"""Typed failure categories and user-safe terminal run messages."""

from __future__ import annotations

from collections.abc import Mapping
from enum import Enum
from typing import Any, TypedDict

from brain.platform.integrations.provider_error_sentinel import provider_error_kind
from brain.platform.integrations.providers import is_transient_transport_disconnect
from brain.contracts.statuses import project_run_status_value
from brain.systems.runs.status import RunStatus, coerce_run_status


class RunFailureCategory(str, Enum):
    INTERNAL = "internal"
    UPSTREAM = "upstream"
    VERIFICATION = "verification"
    PRESERVATION_SETUP = "preservation_setup"


class PublicRunFailure(TypedDict):
    status: str
    category: str
    message: str


DEFAULT_FAILED_RUN_MESSAGE = "I failed on this and it is still open — I will come back."
UPSTREAM_FAILED_RUN_MESSAGE = (
    "I hit a temporary upstream problem on this and it is still open — I will come back."
)
VERIFICATION_FAILED_RUN_MESSAGE = (
    "I couldn't safely verify this and it is still open — I will come back."
)
PRESERVATION_SETUP_FAILED_RUN_MESSAGE = (
    "Illo could not start the preservation workflow before a durable-storage tool ran. "
    "Retry this submission with the same idempotency key. If it fails again, check the "
    "run provider and preservation-tool configuration."
)
CANCELED_RUN_MESSAGE = (
    "That run was canceled before it finished, but the ask is still open — I will come back."
)
EXPIRED_RUN_MESSAGE = (
    "That run timed out before it finished, but the ask is still open — I will come back."
)


def coerce_failure_category(value: RunFailureCategory | str | None) -> RunFailureCategory:
    if isinstance(value, RunFailureCategory):
        return value
    candidate = str(value or "").strip().lower()
    for category in RunFailureCategory:
        if candidate == category.value:
            return category
    return RunFailureCategory.INTERNAL


def failure_category_for_error(error: BaseException | str | None) -> RunFailureCategory:
    if is_transient_transport_disconnect(error) or provider_error_kind(error):
        return RunFailureCategory.UPSTREAM
    return RunFailureCategory.INTERNAL


def run_requires_durable_preservation(metadata: Mapping[str, Any] | None) -> bool:
    metadata = metadata if isinstance(metadata, Mapping) else {}
    submission = metadata.get("submission")
    submission = submission if isinstance(submission, Mapping) else {}
    preservation = submission.get("preservation")
    preservation = preservation if isinstance(preservation, Mapping) else {}
    return preservation.get("requires_durable_evidence") is True


def failure_category_for_run_context(
    category: RunFailureCategory | str | None,
    *,
    metadata: Mapping[str, Any] | None,
    tool_execution_started: bool,
    failure_stage: Any,
) -> RunFailureCategory:
    """Classify an internal pre-tool preservation failure as actionable setup work."""

    resolved = coerce_failure_category(category)
    stage = str(getattr(failure_stage, "value", failure_stage) or "")
    if (
        resolved == RunFailureCategory.INTERNAL
        and run_requires_durable_preservation(metadata)
        and not tool_execution_started
        and stage
        in {
            "runner_execution",
            "project_context_materialization",
            "recipe_execution",
            "agent_execution",
        }
    ):
        return RunFailureCategory.PRESERVATION_SETUP
    return resolved


def safe_terminal_run_message(
    status: RunStatus | str | None,
    category: RunFailureCategory | str | None = None,
) -> str | None:
    normalized_status = (
        status
        if isinstance(status, RunStatus)
        else project_run_status_value(status, RunStatus.FAILED.value)
    )
    run_status = coerce_run_status(normalized_status, default=RunStatus.FAILED)
    if run_status == RunStatus.COMPLETED:
        return None
    if run_status == RunStatus.CANCELED:
        return CANCELED_RUN_MESSAGE
    if run_status == RunStatus.EXPIRED:
        return EXPIRED_RUN_MESSAGE

    failure_category = coerce_failure_category(category)
    if failure_category == RunFailureCategory.UPSTREAM:
        return UPSTREAM_FAILED_RUN_MESSAGE
    if failure_category == RunFailureCategory.VERIFICATION:
        return VERIFICATION_FAILED_RUN_MESSAGE
    if failure_category == RunFailureCategory.PRESERVATION_SETUP:
        return PRESERVATION_SETUP_FAILED_RUN_MESSAGE
    return DEFAULT_FAILED_RUN_MESSAGE


def terminal_run_notice_condition(
    status: RunStatus | str | None,
    category: RunFailureCategory | str | None = None,
) -> str | None:
    """Return a stable deduplication key for one terminal public condition."""

    normalized_status = (
        status
        if isinstance(status, RunStatus)
        else project_run_status_value(status, RunStatus.FAILED.value)
    )
    run_status = coerce_run_status(normalized_status, default=RunStatus.FAILED)
    if run_status == RunStatus.COMPLETED:
        return None
    return f"terminal:{run_status.value}:{coerce_failure_category(category).value}"


def public_run_failure(
    status: RunStatus | str | None,
    category: RunFailureCategory | str | None = None,
) -> PublicRunFailure | None:
    """Return the canonical public representation of a terminal run failure."""

    normalized_status = (
        status
        if isinstance(status, RunStatus)
        else project_run_status_value(status, RunStatus.FAILED.value)
    )
    run_status = coerce_run_status(normalized_status, default=RunStatus.FAILED)
    message = safe_terminal_run_message(run_status, category)
    if message is None:
        return None
    return {
        "status": run_status.value,
        "category": coerce_failure_category(category).value,
        "message": message,
    }


__all__ = [
    "CANCELED_RUN_MESSAGE",
    "DEFAULT_FAILED_RUN_MESSAGE",
    "EXPIRED_RUN_MESSAGE",
    "PublicRunFailure",
    "PRESERVATION_SETUP_FAILED_RUN_MESSAGE",
    "RunFailureCategory",
    "UPSTREAM_FAILED_RUN_MESSAGE",
    "VERIFICATION_FAILED_RUN_MESSAGE",
    "coerce_failure_category",
    "failure_category_for_error",
    "failure_category_for_run_context",
    "public_run_failure",
    "run_requires_durable_preservation",
    "safe_terminal_run_message",
    "terminal_run_notice_condition",
]
