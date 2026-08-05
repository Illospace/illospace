"""Generic effects produced by Cycle pre-admission policies."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class CycleExecutionDisposition(StrEnum):
    """Whether the runner should admit or finalize a Cycle run."""

    ADMIT = "admit"
    FINALIZE = "finalize"


@dataclass(frozen=True)
class CycleExecutionEffect:
    """A policy-neutral instruction for the generic Cycle runner."""

    disposition: CycleExecutionDisposition
    admission_metadata_patch: dict[str, Any] = field(default_factory=dict)
    final_status: str | None = None
    final_error: str | None = None
    final_skip_reason: str | None = None

    @classmethod
    def admit(
        cls,
        *,
        admission_metadata_patch: dict[str, Any] | None = None,
    ) -> CycleExecutionEffect:
        return cls(
            disposition=CycleExecutionDisposition.ADMIT,
            admission_metadata_patch=dict(admission_metadata_patch or {}),
        )

    @classmethod
    def finalize(
        cls,
        *,
        status: str,
        error: str | None = None,
        skip_reason: str | None = None,
    ) -> CycleExecutionEffect:
        return cls(
            disposition=CycleExecutionDisposition.FINALIZE,
            final_status=status,
            final_error=error,
            final_skip_reason=skip_reason,
        )


__all__ = [
    "CycleExecutionDisposition",
    "CycleExecutionEffect",
]
