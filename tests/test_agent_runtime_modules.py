from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def test_agent_runtime_root_exports_canonical_run_profile_primitives():
    from brain.systems import runs as agent_runtime

    assert agent_runtime.run_execution_profile({"execution_profile": "fast"}) == "fast"
    assert agent_runtime.requested_run_profile({"executionProfile": "deep"}) == "deep"
    assert agent_runtime.run_profile_policy("fast").stream_live_reply is True
    assert agent_runtime.select_run_runtime(
        agent_runtime.run_profile_policy("deep")
    ).run_graph is True


def test_retired_cortex_runtime_modules_stay_out_of_live_paths():
    root = Path(__file__).resolve().parents[1]
    retired_roots = [
        root / "brain/systems/cortex/dispatch",
        root / "brain/orchestration",
    ]
    for retired_root in retired_roots:
        live_sources = [
            path
            for path in retired_root.rglob("*")
            if path.suffix in {".py", ".ts", ".svelte"}
        ] if retired_root.exists() else []
        assert not live_sources, f"{retired_root.relative_to(root)} still has retired source files"

    live_roots = [
        root / "brain/systems/runs",
        root / "brain/systems/runs/cortex",
        root / "brain/app/api/routers/cortex",
        root / "frontend/src/lib/stores",
        root / "frontend/src/lib/api",
    ]
    forbidden = (
        "brain.systems.cortex.dispatch",
        "brain/orchestration",
        "brain.orchestration",
        "cortex/dispatch",
        "agent_status",
        "AGENT_STATUS",
    )
    allowed_tombstones = {
        root / "brain/app/api/routers/cortex/_idea_ops.py",
    }

    for live_root in live_roots:
        for path in live_root.rglob("*"):
            if path.suffix not in {".py", ".ts", ".svelte"}:
                continue
            text = path.read_text(encoding="utf-8")
            for marker in forbidden:
                if path in allowed_tombstones and marker == "agent_status":
                    assert 'publish("agent_status"' not in text
                    assert "publish('agent_status'" not in text
                    continue
                assert marker not in text, f"{path.relative_to(root)} still references retired {marker}"


def test_agent_result_runtime_is_facade_reexport():
    from brain.systems.runs.direct_agent import AgentResult as FacadeAgentResult
    from brain.systems.runs.direct_agent import _TokenAccumulator as FacadeTokenAccumulator
    from brain.systems.runs.direct_agent import _make_result
    from brain.systems.runs.direct_loop.result import AgentResult, _TokenAccumulator

    tokens = _TokenAccumulator(input=10, output=5, cache_read=3, cache_creation=2)
    result = _make_result(
        "done",
        True,
        "session-1",
        tokens,
        0,
        ["read_file"],
        worker_results=[{"worker": "ok"}],
    )

    assert FacadeAgentResult is AgentResult
    assert FacadeTokenAccumulator is _TokenAccumulator
    assert result == AgentResult(
        output="done",
        success=True,
        session_id="session-1",
        tokens_input=10,
        tokens_output=5,
        tokens_cache_read=3,
        tokens_cache_creation=2,
        duration_sec=result.duration_sec,
        tool_calls=["read_file"],
        worker_results=[{"worker": "ok"}],
    )


def test_request_runtime_preserves_cache_policy_and_facade_wrappers():
    from brain.systems.runs.direct_agent import (
        _apply_anthropic_cache_breakpoint,
        _apply_provider_system_cache_policy,
        _build_api_request,
        _build_system_blocks,
        _derive_openai_cache_key,
        _derive_prompt_cache_key,
        _get_extended_prompt_cache_retention,
        _get_openai_cache_retention,
        _infer_provider_operation_type,
        _mark_tools_cacheable,
        _response_has_text,
    )
    from brain.systems.runs.direct_loop import request as runtime_request

    llm = SimpleNamespace()
    system = _build_system_blocks(llm, "Be precise", cache=True)
    assert system == [{"type": "text", "text": "Be precise"}]
    assert _apply_anthropic_cache_breakpoint(system, True)[-1]["cache_control"] == {"type": "ephemeral"}
    anthropic_system = _apply_provider_system_cache_policy("anthropic", "Be precise", True)
    assert anthropic_system == [{"type": "text", "text": "Be precise", "cache_control": {"type": "ephemeral"}}]
    assert _apply_provider_system_cache_policy("openai", "Be precise", True) == "Be precise"

    tools = [{"name": "read_file"}, {"name": "write_file"}]
    cacheable = _mark_tools_cacheable(tools)
    assert "cache_control" not in tools[-1]
    assert cacheable[-1]["cache_control"] == {"type": "ephemeral"}

    cache_key = _derive_openai_cache_key(
        "coordinator-idea-12345678-1234-5678-90ab-cdef12345678",
        system,
        tools,
        True,
        operation_type="coordinator",
    )
    assert len(cache_key) <= 64
    assert cache_key.startswith("illo:coordinator:")
    assert cache_key == _derive_prompt_cache_key(
        "coordinator-idea-12345678-1234-5678-90ab-cdef12345678",
        system,
        tools,
        True,
        operation_type="coordinator",
    )
    assert _get_extended_prompt_cache_retention("openai/gpt-5.5") == "24h"
    assert _get_openai_cache_retention("openai/gpt-5.4") == "24h"
    assert _get_openai_cache_retention("openai/gpt-4.1-mini") == "24h"
    assert _get_openai_cache_retention("openai/gpt-4o-mini") is None

    first_worker_cache_key = _derive_openai_cache_key(
        "agent-run-a-phase-a",
        system,
        tools,
        True,
        operation_type="worker",
    )
    second_worker_cache_key = _derive_openai_cache_key(
        "agent-run-b-phase-b",
        system,
        tools,
        True,
        operation_type="worker",
    )
    assert first_worker_cache_key == second_worker_cache_key

    request = _build_api_request(
        "openai/gpt-4.1-mini",
        [{"role": "user", "content": "hello"}],
        512,
        system,
        tools,
        "low",
        {"x": "1"},
        "openai",
        "cache-session",
        True,
        cache_tools=True,
        operation_type="coordinator",
    )
    assert request.cache_key == runtime_request.derive_openai_cache_key(
        "cache-session",
        system,
        tools,
        True,
        operation_type="coordinator",
    )
    assert request.cache_retention == "24h"
    assert request.max_output_tokens is None
    assert request.extra_headers == {"x": "1"}
    assert request.operation_type == "coordinator"

    assert _infer_provider_operation_type(
        session_id="agent-worker-1",
        tool_call_source="runner",
        metadata={},
    ) == "worker"
    assert _response_has_text(SimpleNamespace(content=[SimpleNamespace(type="text", text=" hi ")]))
    assert not _response_has_text(SimpleNamespace(content=[SimpleNamespace(type="text", text=" ")]))


def test_context_overflow_classifier_recognizes_provider_shapes():
    from brain.systems.runs.direct_loop.context_recovery import (
        context_overflow_payload,
        is_context_overflow_error,
    )

    class ContextError(Exception):
        status_code = 400
        code = "context_length_exceeded"

    exc = ContextError("maximum context length exceeded; request too large")

    assert is_context_overflow_error(exc, provider_name="openai")
    assert is_context_overflow_error(
        RuntimeError(
            "API error: Invalid 'instructions': string too long. "
            "Expected a string with maximum length 1048576, but got a string with length 12256492 instead."
        ),
        provider_name="openai",
    )
    assert context_overflow_payload(exc, provider_name="openai")["provider"] == "openai"
    assert not is_context_overflow_error(RuntimeError("temporarily overloaded"), provider_name="openai")


def test_run_agent_retries_once_after_context_overflow_with_checkpoint(monkeypatch):
    from brain.systems.context.thread_handoff import build_thread_handoff
    from brain.systems.runs.direct_agent import run_agent

    monkeypatch.delenv("AGENT_AUTO_COMPACT_TOKEN_LIMIT", raising=False)
    monkeypatch.delenv("AGENT_MODEL_CONTEXT_WINDOW_TOKENS", raising=False)

    session_messages = [{"role": "user", "content": "Start long task. Must keep branch name exact."}]
    for index in range(18):
        session_messages.append({
            "role": "assistant",
            "content": [{"type": "text", "text": f"completed step {index}"}],
        })
        session_messages.append({"role": "user", "content": f"follow up {index}"})
    stored_handoff, _ = build_thread_handoff(
        previous_handoff=None,
        messages_since=session_messages[:-8],
        total_message_count=len(session_messages) - 8,
        session_id="overflow-retry-session",
    )

    client = MagicMock()
    response = MagicMock()
    response.stop_reason = "end_turn"
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "Recovered."
    text_block.model_dump.return_value = {"type": "text", "text": "Recovered."}
    response.content = [text_block]
    usage = MagicMock()
    usage.input_tokens = 100
    usage.output_tokens = 10
    usage.cache_read_input_tokens = 0
    usage.cache_creation_input_tokens = 0
    response.usage = usage
    checkpoint_response = MagicMock()
    checkpoint_block = MagicMock()
    checkpoint_block.type = "text"
    checkpoint_block.text = (
        '{"active_objective":"Continue after provider context overflow.",'
        '"user_constraints":["Must keep branch name exact."],'
        '"verification_status":"not run"}'
    )
    checkpoint_block.model_dump.return_value = {"type": "text", "text": checkpoint_block.text}
    checkpoint_response.content = [checkpoint_block]
    handoff_response = MagicMock()
    handoff_block = MagicMock()
    handoff_block.type = "text"
    handoff_block.text = '{"active_objective":"Recovered after context overflow.","verification_status":"not run"}'
    handoff_block.model_dump.return_value = {"type": "text", "text": handoff_block.text}
    handoff_response.content = [handoff_block]
    client.messages.create.side_effect = [
        RuntimeError("context_length_exceeded: maximum context length exceeded"),
        checkpoint_response,
        response,
        handoff_response,
    ]

    llm = SimpleNamespace(
        client=client,
        provider="anthropic",
        source="test",
        is_oauth=False,
        extra_headers={},
        token_prefix="sk-test",
        auth_mode="api_key",
        get_extra_headers=lambda: {},
        build_request_headers=lambda **_kwargs: {},
    )

    with patch("brain.systems.runs.direct_agent.async_resolve_llm_client", return_value=llm), \
         patch("brain.systems.runs.direct_agent._load_session", return_value=(session_messages, None)), \
         patch("brain.systems.runs.direct_agent._load_session_handoff", return_value=stored_handoff.to_payload()), \
         patch("brain.systems.runs.direct_agent._save_session"), \
         patch("brain.systems.runs.direct_agent._async_record_api_call"):
        result = run_agent(
            message="Continue and finish.",
            model="claude-sonnet-4-6",
            tools=[],
            persist_session=True,
            cache_system_prompt=False,
            session_id="overflow-retry-session",
            max_turns=1,
    )

    assert result.success
    assert client.messages.create.call_count == 4
    checkpoint_prompt = client.messages.create.call_args_list[1].kwargs["messages"][0]["content"]
    assert "runtime context checkpoint" in checkpoint_prompt.lower()
    retry_messages = client.messages.create.call_args_list[2].kwargs["messages"]
    assert any(
        isinstance(msg.get("content"), str)
        and "Context compaction checkpoint" in msg["content"]
        and "Continue after provider context overflow." in msg["content"]
        and "llm_context_checkpoint_compactor" in msg["content"]
        for msg in retry_messages
    )


def test_run_agent_uses_thread_handoff_but_persists_raw_archive(monkeypatch):
    from brain.systems.context.thread_handoff import build_thread_handoff
    from brain.systems.runs.direct_agent import run_agent

    monkeypatch.delenv("AGENT_THREAD_HANDOFF_RECENT_MESSAGES", raising=False)

    session_messages = [{"role": "user", "content": "Original requirement: keep exact details retrievable."}]
    for index in range(24):
        session_messages.append({"role": "assistant", "content": [{"type": "text", "text": f"old work {index}"}]})
        session_messages.append({"role": "user", "content": f"old follow up {index}"})

    stored_handoff, _ = build_thread_handoff(
        previous_handoff=None,
        messages_since=session_messages[:-8],
        total_message_count=len(session_messages) - 8,
        session_id="handoff-run-session",
    )

    client = MagicMock()
    response = MagicMock()
    response.stop_reason = "end_turn"
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = "Done with handoff."
    text_block.model_dump.return_value = {"type": "text", "text": "Done with handoff."}
    response.content = [text_block]
    usage = MagicMock()
    usage.input_tokens = 100
    usage.output_tokens = 10
    usage.cache_read_input_tokens = 0
    usage.cache_creation_input_tokens = 0
    response.usage = usage
    handoff_response = MagicMock()
    handoff_block = MagicMock()
    handoff_block.type = "text"
    handoff_block.text = (
        '{"active_objective":"Continue the raw-message retrieval work.",'
        '"user_constraints":["Keep exact details retrievable."],'
        '"verification_status":"not run"}'
    )
    handoff_block.model_dump.return_value = {"type": "text", "text": handoff_block.text}
    handoff_response.content = [handoff_block]
    client.messages.create.side_effect = [response, handoff_response]

    llm = SimpleNamespace(
        client=client,
        provider="anthropic",
        source="test",
        is_oauth=False,
        extra_headers={},
        token_prefix="sk-test",
        auth_mode="api_key",
        get_extra_headers=lambda: {},
        build_request_headers=lambda **_kwargs: {},
    )

    with patch("brain.systems.runs.direct_agent.async_resolve_llm_client", return_value=llm), \
         patch("brain.systems.runs.direct_agent._load_session", return_value=(session_messages, None)), \
         patch("brain.systems.runs.direct_agent._load_session_handoff", return_value=stored_handoff.to_payload()), \
         patch("brain.systems.runs.direct_agent._save_session") as save_session, \
         patch("brain.systems.runs.direct_agent._save_session_handoff") as save_handoff, \
         patch("brain.systems.runs.direct_agent._async_record_api_call"):
        result = run_agent(
            message="Continue with the next step.",
            model="claude-sonnet-4-6",
            tools=[],
            persist_session=True,
            cache_system_prompt=False,
            session_id="handoff-run-session",
            max_turns=1,
        )

    assert result.success
    assert client.messages.create.call_count == 2
    sent_messages = client.messages.create.call_args_list[0].kwargs["messages"]
    assert "Durable thread handoff summary" in sent_messages[0]["content"]
    assert len(sent_messages) < len(session_messages)
    handoff_prompt = client.messages.create.call_args_list[1].kwargs["messages"][0]["content"]
    assert "durable thread handoff checkpoint" in handoff_prompt.lower()

    saved_messages = save_session.call_args.args[1]
    assert len(saved_messages) == len(session_messages) + 2
    assert saved_messages[0]["content"] == "Original requirement: keep exact details retrievable."
    assert saved_messages[-2]["content"] == "Continue with the next step."
    assert save_handoff.called
    saved_handoff = save_handoff.call_args.args[1]
    assert saved_handoff["source"] == "llm_thread_handoff_compactor"
    assert saved_handoff["checkpoint"]["active_objective"] == "Continue the raw-message retrieval work."


def test_final_reply_checker_runtime_returns_dict_compatible_review():
    from brain.systems.runs.direct_loop.final_reply_checker import (
        FinalReplyReview,
        review_candidate_final_reply,
        review_final_reply_once,
    )

    response = SimpleNamespace(content=[SimpleNamespace(type="text", text='{"status":"resolved","rationale":"done","missing_requirements":[]}')])
    requests = []

    def create(request):
        requests.append(request)
        return response

    provider = SimpleNamespace(create=create)
    llm = SimpleNamespace(
        provider="openai",
        build_request_headers=lambda session_id: {"session_id": session_id},
    )

    review = review_candidate_final_reply(
        user_request="Finish it",
        candidate_output="Done.",
        intent_profile={
            "intent_type": "broad_refactor",
            "completion_mode": "strict_contract",
            "completion_contract": ["Complete every planned phase."],
        },
        provider=provider,
        llm=llm,
        model="openai/gpt-4o-mini",
        session_id="sess-1",
        extract_text=lambda messages: messages[0]["content"][0]["text"],
        content_to_dicts=lambda content: [{"type": "text", "text": content[0].text}],
    )

    assert review == {
        "status": "resolved",
        "approved": True,
        "rationale": "done",
        "missing_requirements": [],
        "raw_output": '{"status":"resolved","rationale":"done","missing_requirements":[]}',
    }
    assert "strict_contract" in requests[0].messages[0]["content"]
    assert "Complete every planned phase" in requests[0].messages[0]["content"]
    assert FinalReplyReview.from_payload(
        {"status": "blocked_on_user", "rationale": "needs key", "missing_requirements": ["API key"]},
        "raw",
    ).to_dict() == {
        "status": "blocked_on_user",
        "approved": True,
        "rationale": "needs key",
        "missing_requirements": ["API key"],
        "raw_output": "raw",
    }

    ctx = SimpleNamespace()
    calls = []

    def reviewer(**kwargs):
        calls.append(kwargs["candidate_output"])
        return {"status": "continue", "approved": False, "rationale": "more", "missing_requirements": [], "raw_output": ""}

    first = review_final_reply_once(
        user_request="Finish",
        candidate_output="More soon",
        agent_context=ctx,
        review_candidate=reviewer,
    )
    second = review_final_reply_once(
        user_request="Finish",
        candidate_output="More   soon",
        agent_context=ctx,
        review_candidate=reviewer,
    )
    assert first is second
    assert calls == ["More soon"]

    third = review_final_reply_once(
        user_request="Finish",
        candidate_output="More soon",
        execution_context="New evidence context",
        agent_context=ctx,
        review_candidate=reviewer,
    )
    assert third is not first
    assert calls == ["More soon", "More soon"]


def test_final_reply_helpers_parse_json_and_cache_review():
    from brain.systems.runs.direct_loop.final_reply import (
        cache_final_reply_review,
        cached_final_reply_review,
        continuation_gate_nudge,
        extract_latest_user_intent,
        parse_checker_payload,
    )

    payload = parse_checker_payload('{"status": "resolved", "missing_requirements": "none", "rationale": "done"}')
    ask_payload = parse_checker_payload('{"decision": "ask_user", "missing_requirements": ["API key"]}')

    assert payload["status"] == "resolved"
    assert payload["missing_requirements"] == ["none"]
    assert ask_payload["status"] == "blocked_on_user"
    assert extract_latest_user_intent("wrapper\nLatest user message:\nShip it") == "Ship it"
    assert "Ship it" in continuation_gate_nudge("Latest user message:\nShip it")

    ctx = SimpleNamespace()
    review = {"status": "resolved"}
    cache_final_reply_review(ctx, "  Done   now ", review)
    assert cached_final_reply_review(ctx, "Done now") is review


def test_gate_runtime_blocks_side_effects_until_brain_context():
    from brain.systems.runs.direct_loop.gates import GateState, check_gate_violations

    brain_violation = check_gate_violations(
        "write_file",
        "tool-2",
        GateState(brain=False),
        {},
        gated_tool_names=frozenset({"write_file"}),
    )
    assert "Brain gate" in brain_violation["content"]


def test_tool_execution_runtime_runs_parallel_safe_handlers_in_order():
    from brain.systems.runs.direct_loop.gates import GateState, check_gate_violations
    from brain.systems.runs.direct_loop.tool_execution import execute_tool_calls

    calls = []

    def handler(value):
        calls.append(value)
        return {"value": value}

    response = SimpleNamespace(
        content=[
            SimpleNamespace(type="tool_use", id="a", name="read_file", input={"value": "first"}),
            SimpleNamespace(type="tool_use", id="b", name="read_file", input={"value": "second"}),
        ]
    )
    made = []
    results = execute_tool_calls(
        response,
        {"read_file": handler},
        made,
        GateState(brain=True),
        None,
        None,
        None,
        "test",
        agent_context=SimpleNamespace(),
        brain_tool_names=frozenset(),
        gated_tool_names=frozenset(),
        research_tool_names=frozenset(),
        research_budget=6,
        parallel_safe_tool_names=frozenset({"read_file"}),
        max_parallel_tool_calls=2,
        check_gate_violations=check_gate_violations,
    )

    assert made == ["read_file", "read_file"]
    assert sorted(calls) == ["first", "second"]
    assert [item["tool_use_id"] for item in results] == ["a", "b"]
    assert '"first"' in results[0]["content"]


def test_tool_execution_preserves_structured_model_content_without_logging_hidden_payload():
    from brain.systems.runs.direct_loop.gates import GateState, check_gate_violations
    from brain.systems.runs.direct_loop.tool_execution import execute_tool_calls

    model_content = [
        {"type": "text", "text": "Observed current browser viewport."},
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": "abc123"},
        },
    ]
    callback_results = []

    def handler():
        return {"ok": True, "_tool_result_content": model_content}

    response = SimpleNamespace(
        content=[
            SimpleNamespace(type="tool_use", id="call_1", name="browser", input={}),
        ]
    )

    results = execute_tool_calls(
        response,
        {"browser": handler},
        [],
        GateState(brain=True),
        lambda _name, _tool_input, result_text: callback_results.append(result_text),
        None,
        None,
        "test",
        agent_context=SimpleNamespace(),
        brain_tool_names=frozenset(),
        gated_tool_names=frozenset(),
        research_tool_names=frozenset(),
        research_budget=6,
        parallel_safe_tool_names=frozenset(),
        max_parallel_tool_calls=1,
        check_gate_violations=check_gate_violations,
    )

    assert results == [{
        "type": "tool_result",
        "tool_use_id": "call_1",
        "content": model_content,
    }]
    assert callback_results == ['{"ok": true}']


def test_tool_execution_limit_one_keeps_handlers_on_current_thread(monkeypatch):
    from brain.systems.runs.direct_loop.gates import GateState, check_gate_violations
    from brain.systems.runs.direct_loop.tool_execution import execute_tool_calls

    monkeypatch.setenv("AGENT_TOOL_TIMEOUT_SECONDS", "0")
    main_thread_id = threading.get_ident()
    seen_threads = []

    def handler(value):
        seen_threads.append(threading.get_ident())
        return {"value": value}

    response = SimpleNamespace(
        content=[
            SimpleNamespace(type="tool_use", id="a", name="read_file", input={"value": "first"}),
            SimpleNamespace(type="tool_use", id="b", name="read_file", input={"value": "second"}),
        ]
    )
    results = execute_tool_calls(
        response,
        {"read_file": handler},
        [],
        GateState(brain=True),
        None,
        None,
        None,
        "test",
        agent_context=SimpleNamespace(),
        brain_tool_names=frozenset(),
        gated_tool_names=frozenset(),
        research_tool_names=frozenset(),
        research_budget=6,
        parallel_safe_tool_names=frozenset({"read_file"}),
        max_parallel_tool_calls=1,
        check_gate_violations=check_gate_violations,
    )

    assert [item["tool_use_id"] for item in results] == ["a", "b"]
    assert seen_threads == [main_thread_id, main_thread_id]


def test_agent_execution_context_is_task_local_for_parallel_tools():
    from brain.systems.runs.execution_context import _agent_context, bind_agent_context

    async def observe(label):
        with bind_agent_context({"idea_id": label}):
            await asyncio.sleep(0)
            return _agent_context.idea_id

    async def run():
        with bind_agent_context({"idea_id": "parent"}):
            observed = await asyncio.gather(observe("first"), observe("second"))
            return observed, _agent_context.idea_id

    observed, parent_idea_id = asyncio.run(run())

    assert sorted(observed) == ["first", "second"]
    assert parent_idea_id == "parent"


def test_async_tool_execution_keeps_parallel_tool_contexts_isolated():
    from brain.systems.runs.direct_loop.gates import GateState, check_gate_violations
    from brain.systems.runs.direct_loop.tool_execution import async_execute_tool_calls
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

    async def run():
        response = SimpleNamespace(
            content=[
                SimpleNamespace(type="tool_use", id="a", name="read_file", input={"value": "first"}),
                SimpleNamespace(type="tool_use", id="b", name="read_file", input={"value": "second"}),
            ]
        )
        with bind_agent_context({"idea_id": "parent", "execution_metadata": {"shared": []}}):
            results = await async_execute_tool_calls(
                response,
                {"read_file": handler},
                [],
                GateState(brain=True),
                None,
                None,
                None,
                "test",
                agent_context=_agent_context,
                brain_tool_names=frozenset(),
                gated_tool_names=frozenset(),
                research_tool_names=frozenset(),
                research_budget=6,
                parallel_safe_tool_names=frozenset({"read_file"}),
                max_parallel_tool_calls=2,
                check_gate_violations=check_gate_violations,
            )
            return results, list(_agent_context.execution_metadata["shared"])

    results, parent_shared = asyncio.run(run())

    payloads = [json.loads(item["content"]) for item in results]
    assert [item["tool_use_id"] for item in results] == ["a", "b"]
    assert payloads == [
        {"marker": "first", "shared": ["first"]},
        {"marker": "second", "shared": ["second"]},
    ]
    assert parent_shared == []




def test_retry_runtime_streams_when_live_callbacks_are_present():
    from brain.systems.runs.direct_loop.retry import api_call_with_retry

    def _unexpected_create(_request):
        raise AssertionError("create should not be used for live fast runs")

    provider = SimpleNamespace(
        create=_unexpected_create,
        is_retryable_error=lambda _exc: False,
    )
    streamed = []

    def streaming_call(provider_arg, request, cancel_event, on_stream_activity, on_stream_delta, **kwargs):
        streamed.append((provider_arg, request, cancel_event.is_set(), on_stream_delta))
        on_stream_delta("Hi")
        return "streamed-response"

    response = api_call_with_retry(
        provider,
        request=SimpleNamespace(),
        llm=SimpleNamespace(is_oauth=False, build_request_headers=lambda **_: {}),
        cancel_event=None,
        on_stream_activity=None,
        on_stream_delta=lambda _delta: None,
        session_id="session-1",
        turn=0,
        tokens=SimpleNamespace(),
        start_time=0,
        tool_calls_made=[],
        call_start=0,
        retry_delays=(),
        streaming_call=streaming_call,
        make_cancelled_result=lambda *args, **kwargs: "cancelled",
        degrade_betas=lambda: False,
    )

    assert response == "streamed-response"
    assert streamed and streamed[0][2] is False


def test_retry_runtime_respects_retry_after_header(monkeypatch):
    from brain.systems.runs.direct_loop import retry as retry_module
    from brain.systems.runs.direct_loop.retry import api_call_with_retry

    class RetryableProviderError(Exception):
        response = SimpleNamespace(headers={"Retry-After": "0.25", "x-request-id": "req-1"})

    attempts = 0
    sleeps = []

    def create(_request):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RetryableProviderError("rate limit")
        return "ok"

    provider = SimpleNamespace(
        create=create,
        is_retryable_error=lambda exc: isinstance(exc, RetryableProviderError),
    )
    monkeypatch.setattr(retry_module, "_blocking_delay", lambda delay: sleeps.append(delay))

    response = api_call_with_retry(
        provider,
        request=SimpleNamespace(),
        llm=SimpleNamespace(is_oauth=False, build_request_headers=lambda **_: {}),
        cancel_event=None,
        on_stream_activity=None,
        on_stream_delta=None,
        session_id="session-1",
        turn=0,
        tokens=SimpleNamespace(),
        start_time=0,
        tool_calls_made=[],
        call_start=0,
        retry_delays=(10,),
        streaming_call=lambda *args, **kwargs: "unused",
        make_cancelled_result=lambda *args, **kwargs: "cancelled",
        degrade_betas=lambda: False,
    )

    assert response == "ok"
    assert sleeps == [0.25]

def test_streaming_runtime_surfaces_public_reflection_not_raw_reasoning(monkeypatch):
    from brain.platform.integrations.transports.base import LLMResponse, StreamContext, StreamEvent, Usage
    from brain.systems.runs.direct_loop import streaming as runtime_streaming

    final = LLMResponse(content=[], stop_reason="end_turn", usage=Usage())
    events = iter([
        StreamEvent(type="thinking", thinking="raw private chain of thought"),
        StreamEvent(type="reflection", text="Checking run state and waiting for the first tool."),
        StreamEvent(type="text", text="Visible answer"),
    ])
    provider = SimpleNamespace(stream=lambda _request: StreamContext(events, final_message=final))
    cancel_event = SimpleNamespace(is_set=lambda: False)
    activities = []
    deltas = []
    times = iter([0, 4, 8, 12])

    monkeypatch.setattr(runtime_streaming.time, "time", lambda: next(times))

    response = runtime_streaming.streaming_call(
        provider,
        request=SimpleNamespace(),
        cancel_event=cancel_event,
        on_stream_activity=activities.append,
        on_stream_delta=deltas.append,
        session_id="session-1",
        tokens=SimpleNamespace(),
        start_time=0,
        tool_calls_made=[],
        call_start=0,
        make_cancelled_result=lambda *args, **kwargs: None,
    )

    assert response is final
    assert any("Thinking through the request" in activity for activity in activities)
    assert any("Checking run state" in activity for activity in activities)
    assert not any(activity.startswith("Reflecting:") for activity in activities)
    assert not any("raw private chain of thought" in activity for activity in activities)
    assert deltas == ["Visible answer"]


def test_public_reflection_excerpt_skips_title_only_fragments():
    from brain.systems.runs.direct_loop.streaming import _public_reflection_excerpt

    assert _public_reflection_excerpt("**Understanding project setup** I") == ""
    assert _public_reflection_excerpt("**Understanding project setup** The") == ""
    assert _public_reflection_excerpt(
        "**Understanding project setup** I need to check the project context before changing resources."
    ).startswith("**Understanding project setup**")


def test_streaming_runtime_dedupes_stable_reflection_excerpt(monkeypatch):
    from brain.platform.integrations.transports.base import LLMResponse, StreamContext, StreamEvent, Usage
    from brain.systems.runs.direct_loop import streaming as runtime_streaming

    repeated_prefix = (
        "**Formulating a PR** I'm responding to the user's request about making a pull request. "
        "They want code changes and need me to inspect the project, update files, run checks, "
        "and prepare the branch."
    )
    final = LLMResponse(content=[], stop_reason="end_turn", usage=Usage())
    events = iter([
        StreamEvent(type="reflection", text=repeated_prefix),
        StreamEvent(type="reflection", text=" More internal summary that should not change the public excerpt."),
        StreamEvent(type="reflection", text=" More internal summary that should still not change it."),
        StreamEvent(type="reflection", text=" More internal summary that remains beyond the public cap."),
    ])
    provider = SimpleNamespace(stream=lambda _request: StreamContext(events, final_message=final))
    cancel_event = SimpleNamespace(is_set=lambda: False)
    activities = []
    times = iter([0, 4, 8, 12, 16])

    monkeypatch.setattr(runtime_streaming.time, "time", lambda: next(times))

    response = runtime_streaming.streaming_call(
        provider,
        request=SimpleNamespace(),
        cancel_event=cancel_event,
        on_stream_activity=activities.append,
        on_stream_delta=None,
        session_id="session-1",
        tokens=SimpleNamespace(),
        start_time=0,
        tool_calls_made=[],
        call_start=0,
        make_cancelled_result=lambda *args, **kwargs: None,
    )

    assert response is final
    assert len(activities) == 1
    assert activities[0].startswith("**Formulating a PR**")


def test_streaming_runtime_waits_for_useful_reflection_fragment(monkeypatch):
    from brain.platform.integrations.transports.base import LLMResponse, StreamContext, StreamEvent, Usage
    from brain.systems.runs.direct_loop import streaming as runtime_streaming

    final = LLMResponse(content=[], stop_reason="end_turn", usage=Usage())
    events = iter([
        StreamEvent(type="reflection", text="**Understanding project setup** I"),
        StreamEvent(
            type="reflection",
            text=" need to check the project context before changing resources.",
        ),
    ])
    provider = SimpleNamespace(stream=lambda _request: StreamContext(events, final_message=final))
    cancel_event = SimpleNamespace(is_set=lambda: False)
    activities = []
    times = iter([0, 4, 8])

    monkeypatch.setattr(runtime_streaming.time, "time", lambda: next(times))

    response = runtime_streaming.streaming_call(
        provider,
        request=SimpleNamespace(),
        cancel_event=cancel_event,
        on_stream_activity=activities.append,
        on_stream_delta=None,
        session_id="session-1",
        tokens=SimpleNamespace(),
        start_time=0,
        tool_calls_made=[],
        call_start=0,
        make_cancelled_result=lambda *args, **kwargs: None,
    )

    assert response is final
    assert activities == [
        "**Understanding project setup** I need to check the project context before changing resources."
    ]



def test_workspace_argument_is_accepted_even_without_bound_workspace(monkeypatch):
    from brain.systems.runs.tool_catalog.handlers import composition

    monkeypatch.setattr(
        composition,
        "_handle_read_file",
        lambda path, start_line=None, end_line=None, _workspace=None: {
            "path": path,
            "workspace": _workspace,
            "content": "ok",
        },
    )

    handlers = composition._get_tool_handlers(workspace_root=None, allowed_workspaces=None)
    assert handlers["read_file"](path="README.md", workspace="default") == {
        "path": "README.md",
        "workspace": None,
        "content": "ok",
    }
