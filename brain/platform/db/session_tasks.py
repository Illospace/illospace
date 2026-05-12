"""Helpers for running ORM tasks against the active database session."""
from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, TypeVar

ReturnT = TypeVar("ReturnT")


def _is_unconfigured_mock(value: Any) -> bool:
    return type(value).__module__.startswith("unittest.mock")


async def run_session_task(session: Any, fn: Callable[[Any], ReturnT]) -> ReturnT:
    """Run an ORM task against an async session or a sync test double."""

    bridge = getattr(session, "run_sync", None)
    if callable(bridge) and not _is_unconfigured_mock(bridge):
        result = bridge(fn)
        if inspect.isawaitable(result):
            return await result
        return result
    return fn(session)
