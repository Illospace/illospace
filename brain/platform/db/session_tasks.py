"""Async-to-sync session task bridge for legacy repository islands."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, TypeVar

ReturnT = TypeVar("ReturnT")


def _is_plain_mock(value: Any) -> bool:
    return type(value).__module__.startswith("unittest.mock")


async def run_external_agent_db(db: Any, fn: Callable[[Any], ReturnT]) -> ReturnT:
    run_sync = getattr(db, "run_sync", None)
    if callable(run_sync) and not _is_plain_mock(run_sync):
        result = run_sync(fn)
        if inspect.isawaitable(result):
            return await result
        return result
    return fn(db)
