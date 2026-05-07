"""Cortex projection for agent runs."""

from __future__ import annotations

from typing import Any


def cortex_run_projection(row: Any) -> dict[str, Any]:
    return {
        "type": "run",
        "id": getattr(row, "id", None),
        "run_id": getattr(row, "id", None),
        "thread_id": getattr(row, "thread_id", None),
        "status": getattr(row, "status", None),
        "profile": getattr(row, "profile", None),
        "recipe": getattr(row, "recipe", None),
        "created_at": _iso(getattr(row, "created_at", None)),
        "started_at": _iso(getattr(row, "started_at", None)),
        "completed_at": _iso(getattr(row, "completed_at", None)),
    }


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


__all__ = ["cortex_run_projection"]
