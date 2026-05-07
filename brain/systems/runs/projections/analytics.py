"""Analytics projections for agent runs."""

from __future__ import annotations

from typing import Any


def run_analytics_summary(row: Any) -> dict[str, Any]:
    return {
        "run_id": getattr(row, "id", None),
        "profile": getattr(row, "profile", None),
        "status": getattr(row, "status", None),
    }


__all__ = ["run_analytics_summary"]
