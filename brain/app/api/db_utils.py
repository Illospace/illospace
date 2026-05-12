"""Small DB helpers for async routes that wrap ORM tasks."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

from brain.platform.db.session_tasks import run_session_task

ReturnT = TypeVar("ReturnT")


async def run_db(db: Any, fn: Callable[[Any], ReturnT]) -> ReturnT:
    """Run an ORM function against an async or sync test session."""

    return await run_session_task(db, fn)
