"""Tool execution collaborators for the agent runtime."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import inspect
import json
import logging
import os
from typing import Any, Callable

from brain.platform.async_io import (
    InvocationProbe,
    bind_invocation_probe,
    invoke_maybe_async,
    mark_side_effect_started,
)
from brain.systems.runs.actions import result_failure_summary
from brain.systems.runs.direct_loop.loop_control import LoopControlPolicy, LoopTermination
from brain.systems.runs.execution_context import bind_agent_context, clone_agent_context_mapping
from brain.systems.runs.tool_catalog.registry import (
    action_policy_for_tool,
    get_tool_registration,
    output_budget_chars_for_tool,
)

logger = logging.getLogger("agent")
_background_tool_tasks: set[asyncio.Task[ResolvedToolCall]] = set()

_DEFAULT_TOOL_TIMEOUT_SECONDS = 180.0
_DEFAULT_TOOL_TIMEOUT_GRACE_SECONDS = 5.0
_DEFAULT_TOOL_TIMEOUT_MAX_SECONDS = 900.0
_RECENT_TOOL_RESULT_LIMIT = 8
_RECENT_TOOL_RESULT_PREVIEW_CHARS = 1200
_RECENT_TOOL_ARGS_PREVIEW_CHARS = 400


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
    error_class: str | None = None
    result_content: Any | None = None


@dataclass(frozen=True)
class ToolExecutionResult:
    """Provider-facing results plus the loop policy's typed stop decision."""

    tool_results: list[dict]
    termination: LoopTermination | None


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
        error_class="ToolTimeoutError",
    )


def _structured_error_class(result: Any) -> str | None:
    payload = result
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("error_class")
    return str(value).strip() if value else None


def _must_finish_before_reporting(request: PendingToolCall) -> bool:
    """Return whether a still-running call could make a retry unsafe."""
    registration = get_tool_registration(request.tool_name)
    if registration is None:
        return bool(getattr(request.handler, "_action_manifest_audited", False))
    if action_policy_for_tool(request.tool_name, kwargs=request.tool_input) is None:
        return False
    side_effect = str(getattr(registration.side_effect_class, "value", registration.side_effect_class))
    return side_effect not in {"read_only", "read_only_external"}


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
    if budget <= 0 or len(result_text) <= budget:
        return result_text

    def truncation_note(shown: int) -> str:
        return (
            "\n[System: output exceeded this tool's visible budget and was middle-truncated "
            f"({shown} of {len(result_text)} chars shown). Treat this as INCOMPLETE evidence: absence from "
            "the visible portion is not absence from the data. Before relying on this listing, "
            "re-read with narrower filters (search/status/person), compact format, a smaller limit, "
            "or pagination.]"
        )

    reserved_note = truncation_note(budget)
    body_budget = budget - len(reserved_note)
    if body_budget < 200:
        return _truncate_middle_text(result_text, budget)
    body = _truncate_middle_text(result_text, body_budget)
    return body + truncation_note(len(body))


def _extract_model_visible_tool_content(result: Any) -> tuple[Any, Any | None]:
    if not isinstance(result, dict):
        return result, None
    if "_tool_result_content" not in result:
        return result, None
    cleaned = dict(result)
    model_content = cleaned.pop("_tool_result_content", None)
    return cleaned, model_content


def _resolved_tool_result(request: PendingToolCall, result: Any) -> ResolvedToolCall:
    result, model_content = _extract_model_visible_tool_content(result)
    result_text = json.dumps(result, default=str)
    failure_summary = result_failure_summary(result)
    is_error = failure_summary is not None
    error_class = _structured_error_class(result) if is_error else None
    if is_error and not error_class:
        error_class = "ToolError"

    if request.tool_name == "brain_encode" and failure_summary:
        result_text += (
            "\n\n[System: brain_encode failed. Do not retry brain_encode in this run. "
            "Move on and end your turn unless another required tool remains.]"
        )
    elif request.tool_name == "cortex_reply" and isinstance(result, dict) and (
        result.get("blocked") or result.get("error")
    ):
        if result.get("instruction"):
            result_text += f"\n\n[System: {result['instruction']}]"
    elif request.result_nudge:
        result_text += request.result_nudge

    return ResolvedToolCall(
        block_id=request.block_id,
        tool_name=request.tool_name,
        tool_input=request.tool_input,
        result_text=truncate_tool_result_text(request.tool_name, result_text),
        is_error=is_error,
        error_class=error_class,
        result_content=model_content,
    )


def _failed_tool_result(request: PendingToolCall, exc: Exception) -> ResolvedToolCall:
    error_class = type(exc).__name__
    return ResolvedToolCall(
        block_id=request.block_id,
        tool_name=request.tool_name,
        tool_input=request.tool_input,
        result_text=f"Error [{error_class}]: {exc}",
        is_error=True,
        error_class=error_class,
    )


def _record_agent_tool_result(agent_context, resolved: ResolvedToolCall, result_text: str) -> None:
    if agent_context is None:
        return
    try:
        tool_log = getattr(agent_context, "tool_calls_log", None)
        if not isinstance(tool_log, list):
            tool_log = []
        tool_log.append(resolved.tool_name)
        setattr(agent_context, "tool_calls_log", tool_log)

        try:
            args_preview = json.dumps(resolved.tool_input, sort_keys=True, default=str)
        except Exception:
            args_preview = str(resolved.tool_input)

        recent = getattr(agent_context, "recent_tool_results", None)
        if not isinstance(recent, list):
            recent = []
        recent_result = {
            "tool_name": resolved.tool_name,
            "args_preview": _truncate_middle_text(args_preview, _RECENT_TOOL_ARGS_PREVIEW_CHARS),
            "is_error": bool(resolved.is_error),
            "result_preview": _truncate_middle_text(
                str(result_text or ""),
                _RECENT_TOOL_RESULT_PREVIEW_CHARS,
            ),
        }
        if resolved.error_class:
            recent_result["error_class"] = resolved.error_class
        recent.append(recent_result)
        setattr(agent_context, "recent_tool_results", recent[-_RECENT_TOOL_RESULT_LIMIT:])
    except Exception:
        logger.debug("Failed to record recent tool result for final-reply context", exc_info=True)


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


def _snapshot_threadlocal_context(agent_context) -> dict | None:
    if agent_context is None:
        return None
    return clone_agent_context_mapping(vars(agent_context))


class _BoundAgentContext:
    def __init__(self, agent_context, threadlocal_context: dict | None):
        self.threadlocal_context = threadlocal_context
        self.context_manager = None

    def __enter__(self):
        if self.threadlocal_context is not None:
            self.context_manager = bind_agent_context(self.threadlocal_context)
            return self.context_manager.__enter__()
        return None

    def __exit__(self, exc_type, exc, tb):
        if self.context_manager is not None:
            return self.context_manager.__exit__(exc_type, exc, tb)
        return False


async def async_invoke_tool_handler(
    handler: Callable,
    tool_input: dict,
    agent_context=None,
    threadlocal_context: dict | None = None,
):
    """Execute a tool handler with optional propagated AgentRun context."""
    with _BoundAgentContext(agent_context, threadlocal_context):
        return await invoke_maybe_async(handler, **tool_input)


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
        if not getattr(request.handler, "_illo_marks_side_effect_start", False):
            mark_side_effect_started()
        result = await async_invoke_tool_handler(
            request.handler,
            request.tool_input,
            agent_context=agent_context,
            threadlocal_context=threadlocal_context,
        )
        return _resolved_tool_result(request, result)
    except Exception as exc:
        logger.warning("Tool %s failed: %s", request.tool_name, exc)
        return _failed_tool_result(request, exc)


async def async_resolve_tool_call(
    request: PendingToolCall,
    *,
    agent_context=None,
    threadlocal_context: dict | None = None,
) -> ResolvedToolCall:
    """Execute one tool request with an async watchdog timeout."""
    timeout_seconds = _tool_timeout_seconds(request.tool_name, request.tool_input)
    probe = InvocationProbe()

    async def call_with_probe():
        with bind_invocation_probe(probe):
            return await _async_resolve_tool_call_once(
                request,
                agent_context=agent_context,
                threadlocal_context=threadlocal_context,
            )

    call = call_with_probe()
    if timeout_seconds is None:
        return await call
    must_finish = _must_finish_before_reporting(request)
    task = asyncio.create_task(call, name=f"tool-{request.tool_name}-blocking-boundary")
    try:
        done, _ = await asyncio.wait({task}, timeout=timeout_seconds)
    except asyncio.CancelledError:
        blocking_active, blocking_started = probe.blocking_snapshot()
        blocking_unavoidable = blocking_active and blocking_started
        action_started = probe.side_effect_started and (
            not blocking_active or blocking_started
        )
        if must_finish and action_started:
            _track_background_tool_task(task)
        elif blocking_unavoidable:
            task.cancel()
            _track_background_tool_task(task)
        else:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        raise
    if task in done:
        return task.result()
    blocking_active, blocking_started = probe.blocking_snapshot()
    blocking_unavoidable = blocking_active and blocking_started
    action_started = probe.side_effect_started and (
        not blocking_active or blocking_started
    )
    if must_finish and action_started:
        logger.warning(
            "Tool %s exceeded its %.2fs watchdog; waiting for a definitive action outcome",
            request.tool_name,
            timeout_seconds,
        )
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            _track_background_tool_task(task)
            raise
    task.cancel()
    if blocking_unavoidable:
        _track_background_tool_task(task)
    else:
        try:
            await task
        except asyncio.CancelledError:
            pass
        if task.done() and not task.cancelled():
            return task.result()
    logger.warning("Tool %s timed out after %.2fs", request.tool_name, timeout_seconds)
    return _timeout_result(request, timeout_seconds)


def _track_background_tool_task(task: asyncio.Task[ResolvedToolCall]) -> None:
    if task.done():
        return
    _background_tool_tasks.add(task)
    task.add_done_callback(_background_tool_tasks.discard)


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
        return _resolved_tool_result(request, result)
    except Exception as exc:
        logger.warning("Tool %s failed: %s", request.tool_name, exc)
        return _failed_tool_result(request, exc)


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
    *,
    agent_context=None,
) -> str:
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
    _record_agent_tool_result(agent_context, resolved, callback_result_text)
    if on_tool_call:
        on_tool_call(resolved.tool_name, resolved.tool_input, callback_result_text)
    return callback_result_text


async def async_emit_resolved_tool_call(
    resolved: ResolvedToolCall,
    tool_results: list[dict],
    on_tool_call,
    run_id,
    idea_id,
    tool_call_source: str,
    *,
    agent_context=None,
) -> None:
    """Append a tool result and persist its trace through the async event log."""
    callback_result_text = emit_resolved_tool_call(
        resolved,
        tool_results,
        on_tool_call,
        None,
        None,
        tool_call_source,
        agent_context=agent_context,
    )
    if run_id and idea_id:
        from brain.systems.runs.events import async_record_tool_call

        await async_record_tool_call(
            run_id,
            idea_id,
            resolved.tool_name,
            resolved.tool_input,
            callback_result_text,
            source=tool_call_source,
        )


def execute_parallel_tool_batch(
    pending: list[PendingToolCall],
    *,
    agent_context,
) -> list[ResolvedToolCall]:
    """Run independent tool calls from sync runtime code while preserving output order."""
    if not pending:
        return []

    threadlocal_context = _snapshot_threadlocal_context(agent_context)
    return [
        resolve_tool_call(
            request,
            agent_context=agent_context,
            threadlocal_context=threadlocal_context,
        )
        for request in pending
    ]


async def async_execute_parallel_tool_batch(
    pending: list[PendingToolCall],
    *,
    agent_context,
    max_parallel_tool_calls: int,
) -> list[ResolvedToolCall]:
    """Resolve independent calls concurrently while preserving provider order."""
    if not pending:
        return []

    threadlocal_context = _snapshot_threadlocal_context(agent_context)
    if max_parallel_tool_calls <= 1:
        return [
            await async_resolve_tool_call(
                request,
                agent_context=agent_context,
                threadlocal_context=threadlocal_context,
            )
            for request in pending
        ]

    resolved_calls: list[ResolvedToolCall] = []
    parallelism = max(1, min(max_parallel_tool_calls, len(pending)))
    for start in range(0, len(pending), parallelism):
        chunk = pending[start:start + parallelism]
        tasks: list[tuple[PendingToolCall, asyncio.Task[ResolvedToolCall]]] = []
        for request in chunk:
            task = asyncio.create_task(
                async_resolve_tool_call(
                    request,
                    agent_context=agent_context,
                    threadlocal_context=threadlocal_context,
                )
            )
            tasks.append((request, task))
        try:
            for request, task in tasks:
                try:
                    resolved_calls.append(await task)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("Tool %s failed: %s", request.tool_name, exc)
                    resolved_calls.append(_failed_tool_result(request, exc))
        finally:
            for _, task in tasks:
                if not task.done():
                    task.cancel()

        for _, task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass

    return resolved_calls


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
    loop_control: LoopControlPolicy,
) -> ToolExecutionResult:
    """Execute all tool calls from a provider response."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError("execute_tool_calls cannot run inside an active event loop; await async_execute_tool_calls")

    tool_results: list[dict] = []
    pending_parallel: list[PendingToolCall] = []
    threadlocal_context = _snapshot_threadlocal_context(agent_context)
    termination = loop_control.termination

    def consume_resolved(resolved: ResolvedToolCall) -> None:
        nonlocal termination
        if termination is None:
            termination = loop_control.observe_tool_result(resolved)
        emit_resolved_tool_call(
            resolved,
            tool_results,
            on_tool_call,
            run_id,
            idea_id,
            tool_call_source,
            agent_context=agent_context,
        )

    def flush_parallel_batch() -> None:
        nonlocal pending_parallel
        if not pending_parallel:
            return
        resolved_calls = execute_parallel_tool_batch(
            pending_parallel,
            agent_context=agent_context,
        )
        pending_parallel = []
        for resolved in resolved_calls:
            consume_resolved(resolved)

    for block in response.content:
        if not (hasattr(block, "type") and block.type == "tool_use"):
            continue

        tool_name = block.name
        tool_input = block.input
        tool_calls_made.append(tool_name)

        if termination is not None:
            tool_results.append(termination.skipped_tool_result(block.id))
            continue

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
            same_tool_limit = loop_control.parallel_same_tool_limit(tool_name)
            same_tool_pending = sum(
                request.tool_name == tool_name for request in pending_parallel
            )
            if same_tool_pending >= same_tool_limit:
                flush_parallel_batch()
                if termination is not None:
                    tool_results.append(termination.skipped_tool_result(block.id))
                    continue
            pending_parallel.append(request)
            continue

        flush_parallel_batch()
        if termination is not None:
            tool_results.append(termination.skipped_tool_result(block.id))
            continue
        consume_resolved(resolve_tool_call(
            request,
            agent_context=agent_context,
            threadlocal_context=threadlocal_context,
        ))

    flush_parallel_batch()
    return ToolExecutionResult(tool_results=tool_results, termination=termination)


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
    loop_control: LoopControlPolicy,
) -> ToolExecutionResult:
    """Execute all tool calls from async runtime code."""
    tool_results: list[dict] = []
    pending_parallel: list[PendingToolCall] = []
    threadlocal_context = _snapshot_threadlocal_context(agent_context)
    termination = loop_control.termination

    async def consume_resolved(resolved: ResolvedToolCall) -> None:
        nonlocal termination
        if termination is None:
            termination = loop_control.observe_tool_result(resolved)
        await async_emit_resolved_tool_call(
            resolved,
            tool_results,
            on_tool_call,
            run_id,
            idea_id,
            tool_call_source,
            agent_context=agent_context,
        )

    async def flush_parallel_batch() -> None:
        nonlocal pending_parallel
        if not pending_parallel:
            return
        resolved_calls = await async_execute_parallel_tool_batch(
            pending_parallel,
            agent_context=agent_context,
            max_parallel_tool_calls=max_parallel_tool_calls,
        )
        pending_parallel = []
        for resolved in resolved_calls:
            await consume_resolved(resolved)

    for block in response.content:
        if not (hasattr(block, "type") and block.type == "tool_use"):
            continue

        tool_name = block.name
        tool_input = block.input
        tool_calls_made.append(tool_name)

        if termination is not None:
            tool_results.append(termination.skipped_tool_result(block.id))
            continue

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
            same_tool_limit = loop_control.parallel_same_tool_limit(tool_name)
            same_tool_pending = sum(
                request.tool_name == tool_name for request in pending_parallel
            )
            if same_tool_pending >= same_tool_limit:
                await flush_parallel_batch()
                if termination is not None:
                    tool_results.append(termination.skipped_tool_result(block.id))
                    continue
            pending_parallel.append(request)
            continue

        await flush_parallel_batch()
        if termination is not None:
            tool_results.append(termination.skipped_tool_result(block.id))
            continue
        await consume_resolved(await async_resolve_tool_call(
            request,
            agent_context=agent_context,
            threadlocal_context=threadlocal_context,
        ))

    await flush_parallel_batch()
    return ToolExecutionResult(tool_results=tool_results, termination=termination)
