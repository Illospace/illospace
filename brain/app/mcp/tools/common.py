"""Shared helpers for MCP tool implementations."""
from __future__ import annotations

import inspect
import json
from typing import Any


async def maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def session_execute(session: Any, *args: Any, **kwargs: Any) -> Any:
    return await maybe_await(session.execute(*args, **kwargs))


async def session_flush(session: Any) -> None:
    await maybe_await(session.flush())


def jsonish(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default
    return value


def json_safe(value: Any) -> Any:
    def convert(item: Any) -> str:
        if hasattr(item, "isoformat"):
            return item.isoformat()
        return str(item)

    return json.loads(json.dumps(value, default=convert))


def truncate_text(value: str, max_chars: int) -> tuple[str, bool]:
    if max_chars <= 0:
        max_chars = 12000
    if len(value) <= max_chars:
        return value, False
    return value[:max_chars], True
