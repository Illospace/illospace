"""Helpers for running the legacy direct-agent loop from async recipes."""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

from brain.platform.async_io import run_blocking


def sync_on_loop(loop: asyncio.AbstractEventLoop, async_fn):
    def _call(*args, **kwargs):
        future = asyncio.run_coroutine_threadsafe(async_fn(*args, **kwargs), loop)
        return future.result()

    return _call


def thread_sync_tool_handlers(loop: asyncio.AbstractEventLoop, handlers: dict[str, Any]) -> dict[str, Any]:
    bridged: dict[str, Any] = {}
    for name, handler in handlers.items():
        async def _invoke(_handler=handler, **kwargs):
            result = _handler(**kwargs)
            if inspect.isawaitable(result):
                result = await result
            return result

        bridged[name] = sync_on_loop(loop, _invoke)
    return bridged


async def invoke_direct_agent_threaded(invoke_direct_agent, spec):
    result = await run_blocking(invoke_direct_agent, spec)
    if inspect.isawaitable(result):
        result = await result
    return result


__all__ = ["invoke_direct_agent_threaded", "sync_on_loop", "thread_sync_tool_handlers"]
