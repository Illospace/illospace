"""Structured outcome metadata shared by scheduler jobs and executors."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Mapping


class SchedulerSkipKind(StrEnum):
    """Explain whether a skipped job can succeed without a configuration change."""

    CONFIGURATION = "configuration"
    TRANSIENT = "transient"


def scheduler_skip_kind(result: Mapping[str, Any]) -> SchedulerSkipKind | None:
    """Return the explicit kind for a structured skipped result."""
    if str(result.get("outcome") or "").strip().lower() != "skipped":
        return None
    try:
        return SchedulerSkipKind(str(result.get("skip_kind") or "").strip().lower())
    except ValueError:
        return None


def find_configuration_skip(
    result: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """Find an explicitly classified configuration skip in an aggregate result."""
    if scheduler_skip_kind(result) is SchedulerSkipKind.CONFIGURATION:
        return result
    nested_results = result.get("results")
    if not isinstance(nested_results, list):
        return None
    for nested_result in nested_results:
        if not isinstance(nested_result, Mapping):
            continue
        match = find_configuration_skip(nested_result)
        if match is not None:
            return match
    return None


__all__ = [
    "SchedulerSkipKind",
    "find_configuration_skip",
    "scheduler_skip_kind",
]
