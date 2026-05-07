"""History projections for agent runs."""

from __future__ import annotations

from typing import Any


def run_history_item(row: Any) -> dict[str, Any]:
    return {"run_id": getattr(row, "id", None), "status": getattr(row, "status", None)}


__all__ = ["run_history_item"]
