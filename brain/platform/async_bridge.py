"""Helpers for sync protocol boundaries that call async implementations."""
from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable
from typing import TypeVar

ReturnT = TypeVar("ReturnT")


def run_async_from_sync(awaitable: Awaitable[ReturnT], *, thread_name: str = "async-sync-bridge") -> ReturnT:
    """Resolve an awaitable from a synchronous protocol/CLI boundary.

    Runtime DB code should stay async. This helper is for unavoidable sync
    entrypoints such as MCP handlers, CLI hooks, and legacy agent-loop facades.
    """

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)

    result: dict[str, ReturnT | BaseException] = {}

    def _runner() -> None:
        try:
            result["value"] = asyncio.run(awaitable)
        except BaseException as exc:  # pragma: no cover - defensive boundary
            result["error"] = exc

    thread = threading.Thread(target=_runner, name=thread_name, daemon=True)
    thread.start()
    thread.join()
    if "error" in result:
        raise result["error"]  # type: ignore[misc]
    return result["value"]  # type: ignore[return-value]
