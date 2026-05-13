"""Tool execution collaborators for the agent runtime."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
import json
import logging
import os
from typing import Any, Callable

from brain.systems.runs.tool_catalog.registry import output_budget_chars_for_tool

logger = logging.getLogger("agent")

_DEFAULT_TOOL_TIMEOUT_SECONDS = 180.0
_DEFAULT_TOOL_TIMEOUT_GRACE_SECONDS = 5.0
_DEFAULT_TOOL_TIMEOUT_MAX_SECONDS = 900.0


@dataclass(frozen=True)
class PendingToolCall:
    block_id: str
    tool_name: str
    tool_input: dict
    handler: Callable
    result_nudge: str | None = None


@dataclass(frozen=True)
class ResolvedToolCall:
    block_id: str
    tool_name: str
    tool_input: dict
    result_text: str
    is_error: bool = False
    result_content: Any | None = None


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        logger.warning("Ignoring invalid %s=%r", name, value)
        return default


def _requested_tool_timeout_seconds(tool_input: dict) -> float | None:
    for key in ("timeout_seconds", "timeout_sec", "timeout"):
        value = tool_input.get(key)
        if value is None:
            continue
        try:
            seconds = float(value)
        except (TypeError, ValueError):
            continue
        if seconds > 0:
            return seconds

    value = tool_input.get("timeout_ms")
    if value is None:
        return None
    try:
        milliseconds = float(value)
    except (TypeError, ValueError):
        return None
    if milliseconds > 0:
        return milliseconds / 1000.0
    return None


def _tool_timeout_seconds(tool_name: str, tool_input: dict) -> float | None:
    del tool_name
    configured = _env_float("AGENT_TOOL_TIMEOUT_SECONDS", _DEFAULT_TOOL_TIMEOUT_SECONDS)
    if configured <= 0:
        return None
    requested = _requested_tool_timeout_seconds(tool_input)
    if requested is not None:
        grace = max(0.0, _env_float("AGENT_TOOL_TIMEOUT_GRACE_SECONDS", _DEFAULT_TOOL_TIMEOUT_GRACE_SECONDS))
        configured = max(configured, requested + grace)
    maximum = _env_float("AGENT_TOOL_TIMEOUT_MAX_SECONDS", _DEFAULT_TOOL_TIMEOUT_MAX_SECONDS)
    if maximum > 0:
        configured = min(configured, maximum)
    return max(0.001, configured)


def _format_timeout(seconds: float) -> str:
    if seconds.is_integer():
        return f"{int(seconds)}s"
    return f"{seconds:.2f}s"


def _timeout_result(request: PendingToolCall, timeout_seconds: float) -> ResolvedToolCall:
    timeout_text = _format_timeout(timeout_seconds)
    return ResolvedToolCall(
        block_id=request.block_id,
        tool_name=request.tool_name,
        tool_input=request.tool_input,
        result_text=(
            f"Tool {request.tool_name!r} timed out after {timeout_text}. "
            "The runtime stopped waiting for this tool call. Treat it as failed, "
            "try a narrower or faster tool call if needed, or explain the blocker to the user."
        ),
        is_error=True,
    )


def _truncate_middle_text(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    marker = f"\n...[{len(text) - max_chars} chars truncated by tool output budget]...\n"
    if max_chars <= len(marker):
        return marker[:max_chars]
    head = (max_chars - len(marker)) // 2
    tail = max_chars - len(marker) - head
    return f"{text[:head]}{marker}{text[-tail:] if tail else ''}"


def truncate_tool_result_text(tool_name: str, result_text: str) -> str:
    """Apply the registry output budget to model-visible tool results."""
    budget = output_budget_chars_for_tool(tool_name)
    return _truncate_middle_text(result_text, budget)


def _extract_model_visible_tool_content(result: Any) -> tuple[Any, Any | None]:
    if not isinstance(result, dict):
        return result, None
    if "_tool_result_content" not in result:
        return result, None
    cleaned = dict(result)
    model_content = cleaned.pop("_tool_result_content", None)
    return cleaned, model_content


def _run_sync_boundary(awaitable: Any, *, name: str) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        with asyncio.Runner() as runner:
            return runner.run(awaitable)
    close = getattr(awaitable, "close", None)
    if callable(close):
        close()
    raise RuntimeError(f"{name} cannot run inside an active event loop; await the async tool API")


async def async_run_tool_awaitable(result):
    """Resolve sync or async tool handler outputs from async runtime code."""
    if not inspect.isawaitable(result):
        return result
    return await result


def run_tool_awaitable(result):
    """Resolve sync or async tool handler outputs in the sync agent loop."""
    if not inspect.isawaitable(result):
        return result
    return _run_sync_boundary(result, name="run_tool_awaitable")


class _BoundAgentContext:
    def __init__(self, agent_context, threadlocal_context: dict | None):
        self.agent_context = agent_context
        self.threadlocal_context = threadlocal_context
        self.previous_context = None

    def __enter__(self):
        if self.agent_context is not None and self.threadlocal_context is not None:
            self.previous_context = vars(self.agent_context).copy()
            for key in list(vars(self.agent_context).keys()):
                delattr(self.agent_context, key)
            for key, value in self.threadlocal_context.items():
                setattr(self.agent_context, key, value)

    def __exit__(self, exc_type, exc, tb):
        if self.agent_context is not None and self.previous_context is not None:
            for key in list(vars(self.agent_context).keys()):
                delattr(self.agent_context, key)
            for key, value in self.previous_context.items():
                setattr(self.agent_context, key, value)
        return False


async def async_invoke_tool_handler(
    handler: Callable,
    tool_input: dict,
    agent_context=None,
    threadlocal_context: dict | None = None,
):
    """Execute a tool handler with optional propagated AgentRun context."""
    with _BoundAgentContext(agent_context, threadlocal_context):
        return await async_run_tool_awaitable(handler(**tool_input))


def invoke_tool_handler(
    handler: Callable,
    tool_input: dict,
    agent_context=None,
    threadlocal_context: dict | None = None,
):
    """Execute a tool handler with optional propagated AgentRun context."""
    with _BoundAgentContext(agent_context, threadlocal_context):
        return run_tool_awaitable(handler(**tool_input))


async def _async_resolve_tool_call_once(
    request: PendingToolCall,
    *,
    agent_context=None,
    threadlocal_context: dict | None = None,
) -> ResolvedToolCall:
    """Execute one tool request and normalize success/error handling."""
    try:
        result = await async_invoke_tool_handler(
            request.handler,
            request.tool_input,
            agent_context=agent_context,
            threadlocal_context=threadlocal_context,
        )
        result, model_content = _extract_model_visible_tool_content(result)
        result_text = json.dumps(result, default=str)
        is_error = False
        if request.tool_name == "brain_encode" and isinstance(result, dict) and result.get("error"):
            result_text += (
                "\n\n[System: brain_encode failed. Do not retry brain_encode in this run. "
                "Move on and end your turn unless another required tool remains.]"
            )
            is_error = True
        elif request.tool_name == "cortex_reply" and isinstance(result, dict) and (
            result.get("blocked") or result.get("error")
        ):
            if result.get("instruction"):
                result_text += f"\n\n[System: {result['instruction']}]"
            is_error = True
        elif request.result_nudge:
            result_text += request.result_nudge
        result_text = truncate_tool_result_text(request.tool_name, result_text)
        return ResolvedToolCall(
            block_id=request.block_id,
            tool_name=request.tool_name,
            tool_input=request.tool_input,
            result_text=result_text,
            is_error=is_error,
            result_content=model_content,
        )
    except Exception as exc:
        logger.warning("Tool %s failed: %s", request.tool_name, exc)
        return ResolvedToolCall(
            block_id=request.block_id,
            tool_name=request.tool_name,
            tool_input=request.tool_input,
            result_text=f"Error: {exc}",
            is_error=True,
        )


async def async_resolve_tool_call(
    request: PendingToolCall,
    *,
    agent_context=None,
    threadlocal_context: dict | None = None,
) -> ResolvedToolCall:
    """Execute one tool request with an async watchdog timeout."""
    timeout_seconds = _tool_timeout_seconds(request.tool_name, request.tool_input)
    call = _async_resolve_tool_call_once(
        request,
        agent_context=agent_context,
        threadlocal_context=threadlocal_context,
    )
    if timeout_seconds is None:
        return await call
    try:
        return await asyncio.wait_for(call, timeout=timeout_seconds)
    except TimeoutError:
        logger.warning("Tool %s timed out after %.2fs", request.tool_name, timeout_seconds)
        return _timeout_result(request, timeout_seconds)


def _resolve_tool_call_sync(
    request: PendingToolCall,
    *,
    agent_context=None,
    threadlocal_context: dict | None = None,
) -> ResolvedToolCall:
    """Execute one tool request and normalize success/error handling."""
    try:
        result = invoke_tool_handler(
            request.handler,
            request.tool_input,
            agent_context=agent_context,
            threadlocal_context=threadlocal_context,
        )
        result, model_content = _extract_model_visible_tool_content(result)
        result_text = json.dumps(result, default=str)
        is_error = False
        if request.tool_name == "brain_encode" and isinstance(result, dict) and result.get("error"):
            result_text += (
                "\n\n[System: brain_encode failed. Do not retry brain_encode in this run. "
                "Move on and end your turn unless another required tool remains.]"
            )
            is_error = True
        elif request.tool_name == "cortex_reply" and isinstance(result, dict) and (
            result.get("blocked") or result.get("error")
        ):
            if result.get("instruction"):
                result_text += f"\n\n[System: {result['instruction']}]"
            is_error = True
        elif request.result_nudge:
            result_text += request.result_nudge
        result_text = truncate_tool_result_text(request.tool_name, result_text)
        return ResolvedToolCall(
            block_id=request.block_id,
            tool_name=request.tool_name,
            tool_input=request.tool_input,
            result_text=result_text,
            is_error=is_error,
            result_content=model_content,
        )
    except Exception as exc:
        logger.warning("Tool %s failed: %s", request.tool_name, exc)
        return ResolvedToolCall(
            block_id=request.block_id,
            tool_name=request.tool_name,
            tool_input=request.tool_input,
            result_text=f"Error: {exc}",
            is_error=True,
        )


def resolve_tool_call(
    request: PendingToolCall,
    *,
    agent_context=None,
    threadlocal_context: dict | None = None,
) -> ResolvedToolCall:
    """Execute one tool request from sync runtime code."""
    return _resolve_tool_call_sync(
        request,
        agent_context=agent_context,
        threadlocal_context=threadlocal_context,
    )


def resolve_tool_call_async_boundary(
    request: PendingToolCall,
    *,
    agent_context=None,
    threadlocal_context: dict | None = None,
) -> ResolvedToolCall:
    """Sync boundary for callers that need async timeout behavior."""
    return _run_sync_boundary(
        async_resolve_tool_call(
            request,
            agent_context=agent_context,
            threadlocal_context=threadlocal_context,
        ),
        name="resolve_tool_call_async_boundary",
    )


def emit_resolved_tool_call(
    resolved: ResolvedToolCall,
    tool_results: list[dict],
    on_tool_call,
    run_id,
    idea_id,
    tool_call_source: str,
) -> None:
    """Append tool_result content and record side effects in block order."""
    tool_results.append({
        "type": "tool_result",
        "tool_use_id": resolved.block_id,
        "content": resolved.result_content if resolved.result_content is not None else resolved.result_text,
        **({"is_error": True} if resolved.is_error else {}),
    })
    callback_result_text = resolved.result_text
    if resolved.tool_name == "brain_vault":
        callback_result_text = "[secret redacted]"
    if on_tool_call:
        on_tool_call(resolved.tool_name, resolved.tool_input, callback_result_text)
    if run_id and idea_id:
        from brain.systems.runs.events import record_tool_call

        record_tool_call(
            run_id,
            idea_id,
            resolved.tool_name,
            resolved.tool_input,
            callback_result_text,
            source=tool_call_source,
        )


def execute_parallel_tool_batch(
    pending: list[PendingToolCall],
    tool_results: list[dict],
    on_tool_call,
    run_id,
    idea_id,
    tool_call_source: str,
    *,
    agent_context,
    max_parallel_tool_calls: int,
) -> None:
    """Run independent tool calls from sync runtime code while preserving output order."""
    if not pending:
        return

    threadlocal_context = vars(agent_context).copy() if agent_context is not None else None
    for request in pending:
        emit_resolved_tool_call(
            resolve_tool_call(
                request,
                agent_context=agent_context,
                threadlocal_context=threadlocal_context,
            ),
            tool_results,
            on_tool_call,
            run_id,
            idea_id,
            tool_call_source,
        )


async def async_execute_parallel_tool_batch(
    pending: list[PendingToolCall],
    tool_results: list[dict],
    on_tool_call,
    run_id,
    idea_id,
    tool_call_source: str,
    *,
    agent_context,
    max_parallel_tool_calls: int,
) -> None:
    """Run async-capable independent tool calls concurrently while preserving output order."""
    if not pending:
        return

    threadlocal_context = vars(agent_context).copy() if agent_context is not None else None
    if max_parallel_tool_calls <= 1:
        for request in pending:
            emit_resolved_tool_call(
                await async_resolve_tool_call(
                    request,
                    agent_context=agent_context,
                    threadlocal_context=threadlocal_context,
                ),
                tool_results,
                on_tool_call,
                run_id,
                idea_id,
                tool_call_source,
            )
        return

    tasks = [
        asyncio.create_task(
            async_resolve_tool_call(
                request,
                agent_context=agent_context,
                threadlocal_context=threadlocal_context,
            )
        )
        for request in pending
    ]
    try:
        for request, task in zip(pending, tasks, strict=True):
            try:
                resolved = await task
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Tool %s failed: %s", request.tool_name, exc)
                resolved = ResolvedToolCall(
                    block_id=request.block_id,
                    tool_name=request.tool_name,
                    tool_input=request.tool_input,
                    result_text=f"Error: {exc}",
                    is_error=True,
                )
            emit_resolved_tool_call(
                resolved,
                tool_results,
                on_tool_call,
                run_id,
                idea_id,
                tool_call_source,
            )
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()


def execute_tool_calls(
    response,
    tool_handlers: dict,
    tool_calls_made: list[str],
    gates,
    on_tool_call,
    run_id,
    idea_id,
    tool_call_source: str,
    *,
    agent_context,
    brain_tool_names: frozenset[str],
    gated_tool_names: frozenset[str],
    research_tool_names: frozenset[str],
    research_budget: int,
    parallel_safe_tool_names: frozenset[str],
    max_parallel_tool_calls: int,
    check_gate_violations: Callable,
) -> list[dict]:
    """Execute all tool calls from a provider response."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError("execute_tool_calls cannot run inside an active event loop; await async_execute_tool_calls")

    tool_results: list[dict] = []
    pending_parallel: list[PendingToolCall] = []
    threadlocal_context = vars(agent_context).copy() if agent_context is not None else None

    def flush_parallel_batch() -> None:
        nonlocal pending_parallel
        if not pending_parallel:
            return
        execute_parallel_tool_batch(
            pending_parallel,
            tool_results,
            on_tool_call,
            run_id,
            idea_id,
            tool_call_source,
            agent_context=agent_context,
            max_parallel_tool_calls=max_parallel_tool_calls,
        )
        pending_parallel = []

    for block in response.content:
        if not (hasattr(block, "type") and block.type == "tool_use"):
            continue

        tool_name = block.name
        tool_input = block.input
        tool_calls_made.append(tool_name)

        if tool_name == "brain_encode" and tool_calls_made.count("brain_encode") > 1:
            flush_parallel_batch()
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": (
                    "brain_encode already ran in this agent turn history. "
                    "Do not call it again; end your turn unless you have a different required tool."
                ),
                "is_error": True,
            })
            continue

        if tool_name in brain_tool_names:
            gates.brain = True
        if tool_name == "brain_skills":
            gates.skills = True
        violation = check_gate_violations(
            tool_name,
            block.id,
            gates,
            tool_handlers,
            gated_tool_names=gated_tool_names,
        )
        if violation:
            flush_parallel_batch()
            tool_results.append(violation)
            continue

        handler = tool_handlers.get(tool_name)
        if not handler:
            flush_parallel_batch()
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": f"Unknown tool: {tool_name}",
                "is_error": True,
            })
            continue

        request = PendingToolCall(
            block_id=block.id,
            tool_name=tool_name,
            tool_input=tool_input,
            handler=handler,
        )

        if tool_name in parallel_safe_tool_names:
            pending_parallel.append(request)
            continue

        flush_parallel_batch()
        emit_resolved_tool_call(
            resolve_tool_call(
                request,
                agent_context=agent_context,
                threadlocal_context=threadlocal_context,
            ),
            tool_results,
            on_tool_call,
            run_id,
            idea_id,
            tool_call_source,
        )

    flush_parallel_batch()
    return tool_results


async def async_execute_tool_calls(
    response,
    tool_handlers: dict,
    tool_calls_made: list[str],
    gates,
    on_tool_call,
    run_id,
    idea_id,
    tool_call_source: str,
    *,
    agent_context,
    brain_tool_names: frozenset[str],
    gated_tool_names: frozenset[str],
    research_tool_names: frozenset[str],
    research_budget: int,
    parallel_safe_tool_names: frozenset[str],
    max_parallel_tool_calls: int,
    check_gate_violations: Callable,
) -> list[dict]:
    """Execute all tool calls from async runtime code."""
    tool_results: list[dict] = []
    pending_parallel: list[PendingToolCall] = []
    threadlocal_context = vars(agent_context).copy() if agent_context is not None else None

    async def flush_parallel_batch() -> None:
        nonlocal pending_parallel
        if not pending_parallel:
            return
        await async_execute_parallel_tool_batch(
            pending_parallel,
            tool_results,
            on_tool_call,
            run_id,
            idea_id,
            tool_call_source,
            agent_context=agent_context,
            max_parallel_tool_calls=max_parallel_tool_calls,
        )
        pending_parallel = []

    for block in response.content:
        if not (hasattr(block, "type") and block.type == "tool_use"):
            continue

        tool_name = block.name
        tool_input = block.input
        tool_calls_made.append(tool_name)

        if tool_name == "brain_encode" and tool_calls_made.count("brain_encode") > 1:
            await flush_parallel_batch()
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": (
                    "brain_encode already ran in this agent turn history. "
                    "Do not call it again; end your turn unless you have a different required tool."
                ),
                "is_error": True,
            })
            continue

        if tool_name in brain_tool_names:
            gates.brain = True
        if tool_name == "brain_skills":
            gates.skills = True
        violation = check_gate_violations(
            tool_name,
            block.id,
            gates,
            tool_handlers,
            gated_tool_names=gated_tool_names,
        )
        if violation:
            await flush_parallel_batch()
            tool_results.append(violation)
            continue

        handler = tool_handlers.get(tool_name)
        if not handler:
            await flush_parallel_batch()
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": f"Unknown tool: {tool_name}",
                "is_error": True,
            })
            continue

        request = PendingToolCall(
            block_id=block.id,
            tool_name=tool_name,
            tool_input=tool_input,
            handler=handler,
        )

        if tool_name in parallel_safe_tool_names:
            pending_parallel.append(request)
            continue

        await flush_parallel_batch()
        emit_resolved_tool_call(
            await async_resolve_tool_call(
                request,
                agent_context=agent_context,
                threadlocal_context=threadlocal_context,
            ),
            tool_results,
            on_tool_call,
            run_id,
            idea_id,
            tool_call_source,
        )

    await flush_parallel_batch()
    return tool_results
