"""Structured outcome metadata shared by scheduler jobs and executors."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping


class SchedulerSkipKind(StrEnum):
    """Explain whether a skipped job can succeed without a configuration change."""

    CONFIGURATION = "configuration"
    TRANSIENT = "transient"


@dataclass(frozen=True, slots=True)
class SchedulerConfigurationSkip:
    """One configuration gap classified from a scheduler result."""

    repository: str | None
    reason: str
    payload: Mapping[str, Any]


def scheduler_skip_kind(result: Mapping[str, Any]) -> SchedulerSkipKind | None:
    """Return the explicit kind for a structured skipped result."""
    if str(result.get("outcome") or "").strip().lower() != "skipped":
        return None
    try:
        return SchedulerSkipKind(str(result.get("skip_kind") or "").strip().lower())
    except ValueError:
        return None


def find_configuration_skips(
    result: Mapping[str, Any],
) -> tuple[SchedulerConfigurationSkip, ...]:
    """Return every explicitly classified configuration skip in result order."""
    matches: list[SchedulerConfigurationSkip] = []
    if scheduler_skip_kind(result) is SchedulerSkipKind.CONFIGURATION:
        repository = str(
            result.get("repo") or result.get("repository") or ""
        ).strip()
        matches.append(
            SchedulerConfigurationSkip(
                repository=repository or None,
                reason=str(
                    result.get("reason")
                    or "Job is blocked by missing configuration"
                ),
                payload=result,
            )
        )
    nested_results = result.get("results")
    if not isinstance(nested_results, list):
        return tuple(matches)
    for nested_result in nested_results:
        if not isinstance(nested_result, Mapping):
            continue
        matches.extend(find_configuration_skips(nested_result))
    return tuple(matches)


def configuration_skip_summary(
    skips: tuple[SchedulerConfigurationSkip, ...],
) -> str:
    """Name every classified repository and reason for operator alerts."""
    if not skips:
        raise ValueError("configuration skip summary requires at least one gap")
    if len(skips) == 1 and skips[0].repository is None:
        return skips[0].reason
    return "\n".join(
        (
            "Configuration gaps:",
            *(
                f"- {skip.repository or 'Job'}: {skip.reason}"
                for skip in skips
            ),
        )
    )


__all__ = [
    "SchedulerConfigurationSkip",
    "SchedulerSkipKind",
    "configuration_skip_summary",
    "find_configuration_skips",
    "scheduler_skip_kind",
]
