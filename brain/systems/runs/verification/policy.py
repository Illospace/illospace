"""Verification policy for run recipes."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from brain.systems.runs.domain import RunProfile


class VerificationMode(StrEnum):
    LIGHTWEIGHT = "lightweight"
    BLOCKING = "blocking"
    SKIP = "skip"


def verification_mode_for_run(profile: RunProfile | str, metadata: dict[str, Any] | None = None) -> VerificationMode:
    metadata = metadata if isinstance(metadata, dict) else {}
    if metadata.get("verification") == "skip":
        return VerificationMode.SKIP
    if metadata.get("strict") or metadata.get("verification") == "blocking":
        return VerificationMode.BLOCKING
    return VerificationMode.LIGHTWEIGHT


__all__ = ["VerificationMode", "verification_mode_for_run"]
