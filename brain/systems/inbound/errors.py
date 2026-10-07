"""Validation errors shared by inbound normalization and prompt construction."""

from __future__ import annotations


class InboundValidationError(ValueError):
    """Raised when an inbound envelope or configured projection is invalid."""


class SubmissionSizeError(InboundValidationError):
    """A submission cannot reach the handling run without losing input."""

    def __init__(self, *, field: str, received: int, limit: int, unit: str = "characters") -> None:
        self.details = {
            "code": "submission_too_large",
            "field": field,
            "received": received,
            "limit": limit,
            "unit": unit,
        }
        super().__init__(f"Submission {field} exceeds {limit} {unit} (received {received}).")
