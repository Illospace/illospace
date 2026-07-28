"""Focused tests for live-run activity introspection."""

import copy
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _mock_llm_client(mock_anthropic_client):
    llm = MagicMock()
    llm.client = mock_anthropic_client
    llm.provider = "anthropic"
    llm.source = "org_main"
    llm.is_oauth = False
    llm.extra_headers = {}
    llm.token_prefix = "sk-ant-api03-test"
    llm.get_extra_headers.return_value = {}
    llm.auth_mode = "api_key"
    llm.build_request_headers.return_value = {}
    return llm


def _response(text=None, *, stop_reason="end_turn", tool_use=None):
    response = MagicMock()
    response.stop_reason = stop_reason
    response.content = []
    if tool_use:
        block = MagicMock()
        block.type = "tool_use"
        block.name = tool_use["name"]
        block.input = tool_use["input"]
        block.id = "tool_123"
        block.model_dump.return_value = {
            "type": "tool_use",
            "name": tool_use["name"],
            "input": tool_use["input"],
            "id": "tool_123",
        }
        response.content.append(block)
    if text:
        block = MagicMock()
        block.type = "text"
        block.text = text
        block.model_dump.return_value = {"type": "text", "text": text}
        response.content.append(block)

    response.usage.input_tokens = 1000
    response.usage.output_tokens = 200
    response.usage.cache_read_input_tokens = 500
    response.usage.cache_creation_input_tokens = 100
    return response


async def test_run_activity_uses_token_ledger_and_worker_spawn_events():
    from brain.systems.runs.runtime_activity import load_run_activity

    session = MagicMock()
    session.scalar = AsyncMock(return_value=2)
    uow = MagicMock()
    uow.session = session
    uow.__aenter__ = AsyncMock(return_value=uow)
    uow.__aexit__ = AsyncMock(return_value=False)
    summarize = AsyncMock(return_value={"tokens_total": 987})

    with patch(
        "brain.systems.runs.runtime_activity.UnitOfWork",
        return_value=uow,
    ), patch(
        "brain.systems.runs.runtime_activity.async_summarize_run_usage",
        new=summarize,
    ):
        result = await load_run_activity(42)

    assert result == {"tokens_used": 987, "workers_spawned": 2}
    summarize.assert_awaited_once_with(session, 42)
    worker_count_query = session.scalar.await_args.args[0]
    compiled = worker_count_query.compile()
    query_text = str(compiled)
    assert "agent_run_events.run_id" in query_text
    assert "agent_run_events.event_type" in query_text
    assert "agent_runs.parent_run_id" not in query_text
    assert 42 in compiled.params.values()
    assert "run.worker_spawned" in compiled.params.values()


def test_agent_binds_run_id_without_loading_or_binding_run():
    from brain.systems.runs import direct_agent
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.definitions.run_support import MY_ACTIVITY_TOOL
    from brain.systems.runs.tool_catalog.handlers.activity import _handle_my_activity

    client = MagicMock()
    client.messages.create.side_effect = [
        _response(
            stop_reason="tool_use",
            tool_use={"name": "my_activity", "input": {}},
        ),
        _response("Activity checked."),
    ]
    bound_contexts = []

    def capture_context(attrs):
        bound_contexts.append(dict(attrs))
        return bind_agent_context(attrs)

    load_run_activity = AsyncMock(
        return_value={"tokens_used": 456, "workers_spawned": 2},
    )
    with patch.object(
        direct_agent,
        "async_resolve_llm_client",
        new=AsyncMock(return_value=_mock_llm_client(client)),
    ), patch.object(
        direct_agent,
        "bind_agent_context",
        new=capture_context,
    ), patch.object(
        direct_agent,
        "_async_record_api_call",
        new=AsyncMock(),
    ), patch(
        "brain.systems.runs.tool_catalog.handlers.activity.load_run_activity",
        new=load_run_activity,
    ):
        result = direct_agent.run_agent(
            message="Check this run",
            model="claude-sonnet-4-6",
            tools=[MY_ACTIVITY_TOOL],
            tool_handlers={"my_activity": _handle_my_activity},
            brain_context_preloaded=True,
            persist_session=False,
            max_turns=2,
            run_id=42,
        )

    assert result.success
    assert bound_contexts
    assert bound_contexts[0]["run_id"] == 42
    assert "run" not in bound_contexts[0]
    load_run_activity.assert_awaited_once_with(42)


async def test_my_activity_includes_live_execution_artifacts():
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers.activity import _handle_my_activity

    with bind_agent_context({
        "run_id": 42,
        "start_time": None,
        "reply_contents": [],
        "tool_calls_log": [],
        "execution_artifacts": [
            {"type": "pr", "number": 123, "url": "https://github.com/x/y/pull/123"},
        ],
    }), patch(
        "brain.systems.runs.tool_catalog.handlers.activity.load_run_activity",
        new=AsyncMock(return_value={"tokens_used": 1234, "workers_spawned": 2}),
    ):
        result = await _handle_my_activity()

    assert result["execution_artifacts"][0]["type"] == "pr"
    assert result["execution_artifacts"][0]["number"] == 123
    assert result["tokens_used"] == 1234
    assert result["workers_spawned"] == 2


async def test_my_activity_loads_persisted_artifacts_without_run_object():
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers.activity import _handle_my_activity

    load_artifacts = AsyncMock(
        return_value=[
            {"type": "commit", "sha": "abc1234", "summary": "Fix provenance"},
        ],
    )
    load_activity = AsyncMock(
        return_value={"tokens_used": 200, "workers_spawned": 1},
    )
    with bind_agent_context({
        "run_id": 42,
        "start_time": None,
        "reply_contents": [],
        "tool_calls_log": [],
        "execution_artifacts": [],
        "execution_metadata": {"execution_id": "exec-123", "run_id": 42},
    }), patch(
        "brain.systems.runs.tool_catalog.handlers.activity.load_execution_artifacts",
        new=load_artifacts,
    ), patch(
        "brain.systems.runs.tool_catalog.handlers.activity.load_run_activity",
        new=load_activity,
    ):
        result = await _handle_my_activity()

    assert result["execution_artifacts"] == [
        {"type": "commit", "sha": "abc1234", "summary": "Fix provenance"},
    ]
    assert result["tokens_used"] == 200
    assert result["workers_spawned"] == 1
    load_artifacts.assert_awaited_once_with(execution_id="exec-123")
    load_activity.assert_awaited_once_with(42)


async def test_my_activity_skips_persisted_artifacts_without_execution_id():
    from brain.systems.runs.execution_context import bind_agent_context
    from brain.systems.runs.tool_catalog.handlers.activity import _handle_my_activity

    with bind_agent_context({
        "run_id": 42,
        "start_time": None,
        "reply_contents": [],
        "tool_calls_log": [],
        "execution_artifacts": [],
        "execution_metadata": {"run_id": 42},
    }), patch(
        "brain.systems.runs.tool_catalog.handlers.activity.load_execution_artifacts",
    ) as load_artifacts, patch(
        "brain.systems.runs.tool_catalog.handlers.activity.load_run_activity",
        new=AsyncMock(return_value={"tokens_used": 50, "workers_spawned": 0}),
    ):
        result = await _handle_my_activity()

    assert "execution_artifacts" not in result
    assert result["tokens_used"] == 50
    assert result["workers_spawned"] == 0
    load_artifacts.assert_not_called()


def test_agent_pushes_soft_and_ceiling_budget_notices_once_without_activity_tool(
    monkeypatch,
):
    from brain.kernel import config
    from brain.systems.runs import direct_agent

    monkeypatch.setattr(config, "AGENT_RUN_CUMULATIVE_TOKEN_BUDGET", 1500)
    monkeypatch.setattr(config, "AGENT_RUN_BUDGET_NOTICE_FRACTION", 0.5)
    monkeypatch.setenv("AGENT_MODEL_CONTEXT_WINDOW_TOKENS", "2000")
    monkeypatch.setenv("AGENT_AUTO_COMPACT_TOKEN_LIMIT", "1500")
    monkeypatch.setenv("AGENT_CONTEXT_RESERVED_OUTPUT_TOKENS", "0")
    monkeypatch.setenv("AGENT_CONTEXT_RESERVED_REASONING_TOKENS", "0")
    monkeypatch.setenv("AGENT_CONTEXT_RESERVED_TOOL_TOKENS", "0")
    monkeypatch.setenv("AGENT_CONTEXT_SAFETY_MARGIN_TOKENS", "0")

    client = MagicMock()
    responses = [
        _response(
            stop_reason="tool_use",
            tool_use={"name": "read_file", "input": {"path": f"step-{index}.txt"}},
        )
        for index in range(3)
    ]
    responses.append(_response("Persisted and closing."))
    request_messages = []

    def create(**kwargs):
        request_messages.append(copy.deepcopy(kwargs["messages"]))
        return responses.pop(0)

    client.messages.create.side_effect = create
    load_activity = AsyncMock(side_effect=[
        {"tokens_used": 0, "workers_spawned": 0},
        {"tokens_used": 750, "workers_spawned": 0},
        {"tokens_used": 1500, "workers_spawned": 0},
    ])

    with patch.object(
        direct_agent,
        "async_resolve_llm_client",
        new=AsyncMock(return_value=_mock_llm_client(client)),
    ), patch.object(
        direct_agent,
        "_async_record_api_call",
        new=AsyncMock(),
    ), patch(
        "brain.systems.runs.direct_loop.run_budget_notice.load_run_activity",
        new=load_activity,
    ):
        result = direct_agent.run_agent(
            message="Do a long task",
            model="claude-sonnet-4-6",
            tools=[{
                "name": "read_file",
                "description": "Read a file.",
                "input_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                },
            }],
            tool_handlers={"read_file": MagicMock(return_value={"content": "ok"})},
            brain_context_preloaded=True,
            persist_session=False,
            max_turns=4,
            run_id=42,
        )

    assert result.success
    assert load_activity.await_count == 3
    assert all(call.args == (42,) for call in load_activity.await_args_list)
    final_context = json.dumps(request_messages[-1])
    assert final_context.count("[System run budget notice: soft; run_id=42]") == 1
    assert final_context.count("[System run budget notice: ceiling; run_id=42]") == 1
    assert "750 of 1500 tokens" in final_context
    assert "1 tool call" in final_context
    assert "Wrap up now and persist durable progress" in final_context
    assert "1500 of 1500 tokens" in final_context
    assert "2 tool calls" in final_context
    assert "persist what you have and emit the closing output" in final_context
    assert "my_activity" not in final_context


def test_cumulative_usage_above_context_compaction_but_below_run_budget_gets_no_notice(
    monkeypatch,
):
    from brain.kernel import config
    from brain.systems.runs import direct_agent

    monkeypatch.setattr(config, "AGENT_RUN_CUMULATIVE_TOKEN_BUDGET", 10_000)
    monkeypatch.setattr(config, "AGENT_RUN_BUDGET_NOTICE_FRACTION", 0.5)
    monkeypatch.setenv("AGENT_MODEL_CONTEXT_WINDOW_TOKENS", "2000")
    monkeypatch.setenv("AGENT_AUTO_COMPACT_TOKEN_LIMIT", "1500")
    monkeypatch.setenv("AGENT_CONTEXT_RESERVED_OUTPUT_TOKENS", "0")
    monkeypatch.setenv("AGENT_CONTEXT_RESERVED_REASONING_TOKENS", "0")
    monkeypatch.setenv("AGENT_CONTEXT_RESERVED_TOOL_TOKENS", "0")
    monkeypatch.setenv("AGENT_CONTEXT_SAFETY_MARGIN_TOKENS", "0")

    client = MagicMock()
    request_messages = []

    def create(**kwargs):
        request_messages.append(copy.deepcopy(kwargs["messages"]))
        return _response("Done.")

    client.messages.create.side_effect = create
    load_activity = AsyncMock(
        return_value={"tokens_used": 1600, "workers_spawned": 0},
    )

    with patch.object(
        direct_agent,
        "async_resolve_llm_client",
        new=AsyncMock(return_value=_mock_llm_client(client)),
    ), patch.object(
        direct_agent,
        "_async_record_api_call",
        new=AsyncMock(),
    ), patch(
        "brain.systems.runs.direct_loop.run_budget_notice.load_run_activity",
        new=load_activity,
    ):
        result = direct_agent.run_agent(
            message="Do a short task",
            model="claude-sonnet-4-6",
            tools=[],
            tool_handlers={},
            persist_session=False,
            max_turns=1,
            run_id=42,
        )

    assert result.success
    assert request_messages == [[{"role": "user", "content": "Do a short task"}]]
    load_activity.assert_awaited_once_with(42)


@pytest.mark.parametrize("run_budget", [None, 0, -1])
async def test_unconfigured_run_budget_skips_notices_and_ledger_read(
    monkeypatch,
    run_budget,
):
    from brain.kernel import config
    from brain.systems.runs.direct_loop.run_budget_notice import (
        load_due_budget_notices,
    )

    monkeypatch.setattr(config, "AGENT_RUN_CUMULATIVE_TOKEN_BUDGET", run_budget)
    load_activity = AsyncMock()

    with patch(
        "brain.systems.runs.direct_loop.run_budget_notice.load_run_activity",
        new=load_activity,
    ):
        notices = await load_due_budget_notices(
            run_id=42,
            tool_calls_log=[],
            sent=set(),
        )

    assert notices == ()
    load_activity.assert_not_awaited()


async def test_reloaded_run_with_both_budget_notices_skips_ledger_read(monkeypatch):
    from brain.kernel import config
    from brain.systems.runs.direct_loop.run_budget_notice import (
        budget_notices_seen,
        load_due_budget_notices,
    )

    monkeypatch.setattr(config, "AGENT_RUN_CUMULATIVE_TOKEN_BUDGET", 1500)
    loaded_messages = [
        {
            "role": "user",
            "content": "[System run budget notice: soft; run_id=42] prior notice",
        },
        {
            "role": "user",
            "content": "[System run budget notice: ceiling; run_id=42] prior notice",
        },
    ]
    sent = budget_notices_seen(loaded_messages, run_id=42)
    load_activity = AsyncMock()

    with patch(
        "brain.systems.runs.direct_loop.run_budget_notice.load_run_activity",
        new=load_activity,
    ):
        notices = await load_due_budget_notices(
            run_id=42,
            tool_calls_log=[],
            sent=sent,
        )

    assert sent == {"soft", "ceiling"}
    assert notices == ()
    load_activity.assert_not_awaited()


@pytest.mark.parametrize(
    ("loaded_messages", "run_id", "expected"),
    [
        (
            [{
                "role": "user",
                "content": "[System run budget notice: soft; run_id=42] prior notice",
            }],
            42,
            {"soft"},
        ),
        (
            [{
                "role": "user",
                "content": "[System run budget notice: ceiling; run_id=41] another run",
            }],
            42,
            set(),
        ),
    ],
)
def test_budget_notice_markers_are_scoped_to_the_run(
    loaded_messages,
    run_id,
    expected,
):
    from brain.systems.runs.direct_loop.run_budget_notice import budget_notices_seen

    assert budget_notices_seen(loaded_messages, run_id=run_id) == expected
