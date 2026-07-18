"""Typed failure categories and user-safe terminal run messages."""

from __future__ import annotations

from enum import Enum
from typing import TypedDict

from brain.platform.integrations.provider_error_sentinel import provider_error_kind
from brain.platform.integrations.providers import is_transient_transport_disconnect
from brain.contracts.statuses import project_run_status_value
from brain.systems.runs.status import RunStatus, coerce_run_status


class RunFailureCategory(str, Enum):
    INTERNAL = "internal"
    UPSTREAM = "upstream"
    VERIFICATION = "verification"


class PublicRunFailure(TypedDict):
    status: str
    category: str
    message: str


DEFAULT_FAILED_RUN_MESSAGE = "I hit a temporary problem while working on that — please retry."
UPSTREAM_FAILED_RUN_MESSAGE = "I hit a temporary upstream problem — please retry."
VERIFICATION_FAILED_RUN_MESSAGE = "I couldn't safely verify the result — please retry."
CANCELED_RUN_MESSAGE = "That run was canceled before it finished."
EXPIRED_RUN_MESSAGE = "That run timed out before it finished — please retry."


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
    return DEFAULT_FAILED_RUN_MESSAGE


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
    "RunFailureCategory",
    "UPSTREAM_FAILED_RUN_MESSAGE",
    "VERIFICATION_FAILED_RUN_MESSAGE",
    "coerce_failure_category",
    "failure_category_for_error",
    "public_run_failure",
    "safe_terminal_run_message",
]
