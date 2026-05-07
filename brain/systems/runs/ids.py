"""Run identifier helpers."""

from __future__ import annotations

from uuid import uuid4


def new_trace_id(prefix: str = "run") -> str:
    return f"{prefix}:{uuid4().hex}"


def trace_id_for_run_id(run_id: int | str | None) -> str | None:
    try:
        value = int(run_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return f"run:{value}"


__all__ = ["new_trace_id", "trace_id_for_run_id"]
