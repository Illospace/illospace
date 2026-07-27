"""Focused tests for live-run activity introspection."""

from unittest.mock import AsyncMock, MagicMock, patch


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
