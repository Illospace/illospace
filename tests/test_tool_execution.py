"""Focused policy and concurrency tests for direct-loop tool execution."""

import asyncio
import json
import threading
from dataclasses import asdict
from types import SimpleNamespace

from brain.systems.runs.direct_loop.gates import GateState, check_gate_violations
from brain.systems.runs.direct_loop.loop_control import (
    LoopControlPolicy,
    LoopTerminationReason,
)
from brain.systems.runs.tool_outcomes import (
    DEFAULT_TOOL_FAILURE_CATEGORY,
    ToolHandlerResult,
    ToolFailure,
    ToolOutcome,
)
from brain.systems.runs.direct_loop.tool_execution import (
    PendingToolCall,
    ResolvedToolCall,
    async_emit_resolved_tool_call,
    async_execute_parallel_tool_batch,
    async_execute_tool_calls,
    async_resolve_tool_call,
    classify_tool_result,
    execute_parallel_tool_batch,
    execute_tool_calls,
    resolve_tool_call,
)


def _response(*calls: tuple[str, str, dict]) -> SimpleNamespace:
    return SimpleNamespace(
        content=[
            SimpleNamespace(type="tool_use", id=block_id, name=name, input=tool_input)
            for block_id, name, tool_input in calls
        ]
    )


def _execute_sync(
    response,
    handlers,
    *,
    calls_made=None,
    on_tool_call=None,
    agent_context=None,
    parallel_safe_tool_names=frozenset(),
    max_parallel_tool_calls=1,
    loop_control=None,
):
    return execute_tool_calls(
        response,
        handlers,
        calls_made if calls_made is not None else [],
        GateState(brain=True),
        on_tool_call,
        None,
        None,
        "test",
        agent_context=agent_context or SimpleNamespace(),
        brain_tool_names=frozenset(),
        gated_tool_names=frozenset(),
        research_tool_names=frozenset(),
        research_budget=6,
        parallel_safe_tool_names=parallel_safe_tool_names,
        max_parallel_tool_calls=max_parallel_tool_calls,
        check_gate_violations=check_gate_violations,
        loop_control=loop_control or LoopControlPolicy(),
    )


async def _execute_async(
    response,
    handlers,
    *,
    calls_made=None,
    on_tool_call=None,
    agent_context=None,
    parallel_safe_tool_names=frozenset(),
    max_parallel_tool_calls=1,
    loop_control=None,
):
    return await async_execute_tool_calls(
        response,
        handlers,
        calls_made if calls_made is not None else [],
        GateState(brain=True),
        on_tool_call,
        None,
        None,
        "test",
        agent_context=agent_context or SimpleNamespace(),
        brain_tool_names=frozenset(),
        gated_tool_names=frozenset(),
        research_tool_names=frozenset(),
        research_budget=6,
        parallel_safe_tool_names=parallel_safe_tool_names,
        max_parallel_tool_calls=max_parallel_tool_calls,
        check_gate_violations=check_gate_violations,
        loop_control=loop_control or LoopControlPolicy(),
    )


def test_sync_executor_runs_parallel_safe_handlers_in_provider_order():
    calls = []

    def handler(value):
        calls.append(value)
        return {"value": value}

    calls_made = []
    execution = _execute_sync(
        _response(
            ("a", "read_file", {"value": "first"}),
            ("b", "read_file", {"value": "second"}),
        ),
        {"read_file": handler},
        calls_made=calls_made,
        parallel_safe_tool_names=frozenset({"read_file"}),
        max_parallel_tool_calls=2,
    )

    assert calls_made == ["read_file", "read_file"]
    assert sorted(calls) == ["first", "second"]
    assert [item["tool_use_id"] for item in execution.tool_results] == ["a", "b"]
    assert '"first"' in execution.tool_results[0]["content"]
    assert execution.termination is None


def test_executor_preserves_structured_model_content_without_logging_hidden_payload():
    model_content = [
        {"type": "text", "text": "Observed current browser viewport."},
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": "abc123"},
        },
    ]
    callback_results = []

    execution = _execute_sync(
        _response(("call_1", "browser", {})),
        {"browser": lambda: {"ok": True, "_tool_result_content": model_content}},
        on_tool_call=lambda _name, _input, text: callback_results.append(text),
    )

    assert execution.tool_results == [{
        "type": "tool_result",
        "tool_use_id": "call_1",
        "content": model_content,
    }]
    assert callback_results == ['{"ok": true}']


def test_sync_executor_limit_one_keeps_handlers_on_current_thread(monkeypatch):
    monkeypatch.setenv("AGENT_TOOL_TIMEOUT_SECONDS", "0")
    main_thread_id = threading.get_ident()
    seen_threads = []

    def handler(value):
        seen_threads.append(threading.get_ident())
        return {"value": value}

    execution = _execute_sync(
        _response(
            ("a", "read_file", {"value": "first"}),
            ("b", "read_file", {"value": "second"}),
        ),
        {"read_file": handler},
        parallel_safe_tool_names=frozenset({"read_file"}),
    )

    assert [item["tool_use_id"] for item in execution.tool_results] == ["a", "b"]
    assert seen_threads == [main_thread_id, main_thread_id]


async def test_async_executor_keeps_parallel_tool_contexts_isolated():
    from brain.systems.runs.execution_context import _agent_context, bind_agent_context

    started = []

    async def handler(value):
        _agent_context.tool_marker = value
        _agent_context.execution_metadata["shared"].append(value)
        started.append(value)
        while len(started) < 2:
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        return {
            "marker": _agent_context.tool_marker,
            "shared": list(_agent_context.execution_metadata["shared"]),
        }

    with bind_agent_context({"idea_id": "parent", "execution_metadata": {"shared": []}}):
        execution = await _execute_async(
            _response(
                ("a", "read_file", {"value": "first"}),
                ("b", "read_file", {"value": "second"}),
            ),
            {"read_file": handler},
            agent_context=_agent_context,
            parallel_safe_tool_names=frozenset({"read_file"}),
            max_parallel_tool_calls=2,
        )
        parent_shared = list(_agent_context.execution_metadata["shared"])

    payloads = [json.loads(item["content"]) for item in execution.tool_results]
    assert [item["tool_use_id"] for item in execution.tool_results] == ["a", "b"]
    assert payloads == [
        {"marker": "first", "shared": ["first"]},
        {"marker": "second", "shared": ["second"]},
    ]
    assert parent_shared == []


async def test_batch_helpers_return_resolved_calls_without_observing_policy():
    sync_request = PendingToolCall(
        block_id="sync",
        tool_name="read_file",
        tool_input={},
        handler=lambda: {"ok": True},
    )

    async def async_handler():
        return {"ok": True}

    async_request = PendingToolCall(
        block_id="async",
        tool_name="read_file",
        tool_input={},
        handler=async_handler,
    )

    sync_results = execute_parallel_tool_batch(
        [sync_request],
        agent_context=SimpleNamespace(),
    )
    async_results = await async_execute_parallel_tool_batch(
        [async_request],
        agent_context=SimpleNamespace(),
        max_parallel_tool_calls=2,
    )

    assert all(isinstance(result, ResolvedToolCall) for result in sync_results)
    assert all(isinstance(result, ResolvedToolCall) for result in async_results)


async def test_direct_loop_stamps_registry_side_effect_at_emit(monkeypatch):
    recorded = []

    async def record(*args, **kwargs):
        recorded.append((args, kwargs))

    monkeypatch.setattr("brain.systems.runs.events.async_record_tool_call", record)

    await async_emit_resolved_tool_call(
        ResolvedToolCall(
            block_id="tool-1",
            tool_name="create_github_pull_request",
            tool_input={"repo": "Illospace/illospace"},
            result_text='{"ok": true}',
        ),
        [],
        None,
        42,
        "idea-1",
        "test",
    )

    assert recorded[0][1]["side_effect"] == "append_only"


async def test_failure_policy_stops_parallel_batch_before_fourth_attempt():
    attempts = []

    async def handler(value):
        attempts.append(value)
        await asyncio.sleep(0)
        raise RuntimeError("deterministic failure")

    execution = await _execute_async(
        _response(*[
            (f"call-{index}", "read_file", {"value": "same"})
            for index in range(4)
        ]),
        {"read_file": handler},
        parallel_safe_tool_names=frozenset({"read_file"}),
        max_parallel_tool_calls=4,
    )

    assert len(attempts) == 3
    assert len(execution.tool_results) == 4
    assert all(result["is_error"] is True for result in execution.tool_results)
    assert execution.termination is not None
    assert execution.termination.reason is LoopTerminationReason.TOOL_FAILURE_CIRCUIT
    assert execution.termination.tool_name == "read_file"
    assert execution.termination.error_class == "RuntimeError"
    assert execution.termination.consecutive_failures == 3


def test_failure_policy_stops_typed_result_before_fourth_attempt():
    attempts = []
    handler_result = ToolHandlerResult(
        value=json.dumps({"error": "invalid tool input"}),
        outcome=ToolOutcome.failed(
            message="invalid tool input",
            category="ToolValidationError",
        ),
    )

    def handler():
        attempts.append("attempt")
        return handler_result

    execution = _execute_sync(
        _response(*[
            (f"call-{index}", "manage_idea", {})
            for index in range(4)
        ]),
        {"manage_idea": handler},
    )

    assert len(attempts) == 3
    assert execution.termination is not None
    assert execution.termination.reason is LoopTerminationReason.TOOL_FAILURE_CIRCUIT
    assert execution.termination.error_class == "ToolValidationError"
    assert execution.termination.consecutive_failures == 3


def test_classifier_ignores_legacy_payload_category():
    outcome = classify_tool_result({
        "error": "invalid tool input",
        "error_class": "UndocumentedPayloadCategory",
    })

    assert outcome.failure == ToolFailure(
        message="invalid tool input",
        category=DEFAULT_TOOL_FAILURE_CATEGORY,
    )


def test_tool_handler_result_keeps_outcome_in_identity_and_serialization():
    value = json.dumps({"error": "invalid tool input"})
    failure = ToolHandlerResult(
        value=value,
        outcome=ToolOutcome.failed(
            message="invalid tool input",
            category="ToolValidationError",
        ),
    )
    success = ToolHandlerResult(value=value, outcome=ToolOutcome())

    assert failure != success
    assert len({failure, success}) == 2
    assert asdict(failure) == {
        "value": value,
        "outcome": {
            "failure": {
                "message": "invalid tool input",
                "category": "ToolValidationError",
            },
        },
    }


def test_resolve_tool_call_preserves_typed_failure_outcome():
    handler_result = ToolHandlerResult(
        value=json.dumps({"error": "parent_id must be an existing idea id or omitted"}),
        outcome=ToolOutcome.failed(
            message="parent_id must be an existing idea id or omitted",
            category="ToolValidationError",
        ),
    )
    request = PendingToolCall(
        block_id="tool_419",
        tool_name="manage_idea",
        tool_input={"action": "create"},
        handler=lambda **_: handler_result,
    )

    resolved = resolve_tool_call(request)

    assert resolved.outcome == handler_result.outcome
    assert resolved.outcome.failure is not None
    assert resolved.outcome.failure.category == "ToolValidationError"
    assert resolved.result_value == handler_result.value
    assert resolved.result_text == json.dumps(handler_result.value)
    assert "parent_id must be an existing idea id or omitted" in resolved.result_text


async def test_resolved_failures_preserve_error_class_and_result_value(monkeypatch):
    handler_failure = ToolHandlerResult(
        value=json.dumps({"error": "invalid tool input"}),
        outcome=ToolOutcome.failed(
            message="invalid tool input",
            category="ToolValidationError",
        ),
    )
    returned_failure = resolve_tool_call(PendingToolCall(
        block_id="handler-failure",
        tool_name="manage_idea",
        tool_input={},
        handler=lambda: handler_failure,
    ))

    def raise_failure():
        raise ValueError("handler raised")

    raised_failure = resolve_tool_call(PendingToolCall(
        block_id="raised-failure",
        tool_name="read_file",
        tool_input={},
        handler=raise_failure,
    ))

    async def never_finishes():
        await asyncio.Event().wait()

    monkeypatch.setenv("AGENT_TOOL_TIMEOUT_SECONDS", "0.001")
    timeout_failure = await async_resolve_tool_call(PendingToolCall(
        block_id="timeout-failure",
        tool_name="read_file",
        tool_input={},
        handler=never_finishes,
    ))

    assert returned_failure.outcome.failure is not None
    assert returned_failure.outcome.failure.category == "ToolValidationError"
    assert returned_failure.result_value == handler_failure.value
    assert raised_failure.outcome.failure is not None
    assert raised_failure.outcome.failure.category == "ValueError"
    assert raised_failure.result_value == {"error": "handler raised"}
    assert timeout_failure.outcome.failure is not None
    assert timeout_failure.outcome.failure.category == "ToolTimeoutError"
    assert timeout_failure.result_value == {
        "error": "tool_timeout",
        "timeout_seconds": 0.001,
    }
