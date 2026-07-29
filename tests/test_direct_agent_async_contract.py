from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


class _FakeLLM:
    provider = "anthropic"
    client = object()
    source = "test"
    auth_mode = "api_key"
    token_prefix = "sk-test"
    is_oauth = False

    def build_request_headers(self, **_kwargs):
        return {}


def _response(text: str):
    block = SimpleNamespace(
        type="text",
        text=text,
        model_dump=lambda **_kwargs: {"type": "text", "text": text},
    )
    usage = SimpleNamespace(
        input_tokens=10,
        output_tokens=5,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    return SimpleNamespace(stop_reason="end_turn", content=[block], usage=usage)


def test_provider_error_classifier_recognizes_transient_aliases_and_contextual_5xx():
    from brain.platform.integrations.provider_error_sentinel import (
        provider_error_kind,
    )

    assert provider_error_kind("server_is_overloaded") == "overloaded_error"
    assert provider_error_kind("service_unavailable_error") == "server_error"
    assert provider_error_kind("provider HTTP 503 unavailable") == "server_error"
    assert provider_error_kind("Reader counted 500 matching files.") is None
    assert (
        provider_error_kind("", provider_exception="server_is_overloaded")
        == "overloaded_error"
    )
    assert (
        provider_error_kind("", provider_exception="provider HTTP 503 unavailable")
        == "server_error"
    )


@pytest.mark.parametrize("spawned", [False, True], ids=["non_spawned", "spawned"])
@pytest.mark.parametrize(
    ("tools", "history_safety"),
    [
        (["read_file"], "read_only"),
        (["write_file"], "write"),
        (["unregistered_test_tool"], "unknown"),
    ],
    ids=["read_only_history", "write_history", "unknown_history"],
)
@pytest.mark.parametrize(
    ("response_text", "expected_kind", "transient"),
    [
        ("provider HTTP 503 unavailable", "server_error", True),
        (
            "Contact help.openai.com with request ID req-non-retryable.",
            "provider_error",
            False,
        ),
        ("Cycle run degraded: mission_contract_failed.", None, False),
    ],
    ids=["retryable_text", "non_retryable_text", "internal_failure"],
)
def test_response_text_retry_decision_matrix(
    *,
    spawned,
    tools,
    history_safety,
    response_text,
    expected_kind,
    transient,
):
    from brain.systems.runs.direct_loop.retry import (
        response_text_retry_decision,
    )

    decision = response_text_retry_decision(
        response_text,
        scheduled_result_contract=False,
        metadata={
            "execution_provenance": (
                {"origin": "spawn_worker", "spawned_by_tool": True}
                if spawned
                else {"origin": "slack_teammate"}
            )
        },
        tool_call_source="worker" if spawned else "coordinator",
        tool_calls_made=tools,
    )

    assert tools
    assert decision.spawned_worker is spawned
    assert decision.tool_history_safety == history_safety
    assert decision.inspect_response is spawned
    assert decision.withhold_stream is spawned
    assert decision.provider_error_kind == (
        expected_kind if spawned else None
    )
    assert decision.should_retry is (
        spawned and history_safety == "read_only" and transient
    )


async def test_scheduled_cycle_retries_provider_error_text_before_returning_safe_sentinel(
    monkeypatch,
):
    from brain.systems.runs import direct_agent

    raw_provider_error = (
        "An error occurred while processing your request. Contact help.openai.com with request ID "
        "req-primary-cycle. | server_error | server_error"
    )
    provider_calls = []
    streamed_deltas = []

    class FakeStream:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            yield SimpleNamespace(type="text", text=raw_provider_error)

        def get_final_message(self):
            return _response(raw_provider_error)

    class FakeProvider:
        def stream(self, request):
            provider_calls.append(request)
            return FakeStream()

        def create(self, _request):
            raise AssertionError("Cycle primary calls with activity hooks should use streaming")

        def is_api_error(self, _exc):
            return False

        def is_retryable_error(self, _exc):
            return False

    monkeypatch.setattr(direct_agent, "get_provider", lambda *_args, **_kwargs: FakeProvider())
    monkeypatch.setattr(direct_agent, "_PROVIDER_ERROR_TEXT_RETRY_DELAYS", (0,))

    result = await direct_agent.run_agent_async(
        message="Run the scheduled Cycle mission",
        model="claude-sonnet-4-6",
        tools=[],
        persist_session=False,
        session_id="cycle-provider-error-retry",
        resolved_llm=_FakeLLM(),
        on_stream_activity=lambda _label: None,
        on_stream_delta=streamed_deltas.append,
        metadata={
            "execution_provenance": {
                "source": "cycle",
                "contract": {
                    "kind": "autonomous_cycle_run",
                    "result": {"kind": "autonomous_cycle_run_result"},
                },
            }
        },
    )

    assert len(provider_calls) == 2
    assert result.success is True
    assert result.output == "upstream_provider_error: server_error"
    assert "help.openai.com" not in result.output
    assert streamed_deltas == []


async def test_spawned_read_only_worker_retries_overload_text_and_recovers(
    monkeypatch,
):
    from brain.systems.runs import direct_agent

    raw_provider_error = (
        "API error: Our servers are currently overloaded | "
        "server_is_overloaded | service_unavailable_error"
    )
    provider_calls = []
    streamed_deltas = []

    class FakeStream:
        def __init__(self, text):
            self.text = text

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            yield SimpleNamespace(type="text", text=self.text)

        def get_final_message(self):
            return _response(self.text)

    class FakeProvider:
        def stream(self, request):
            provider_calls.append(request)
            text = (
                raw_provider_error
                if len(provider_calls) == 1
                else "Recovered repository evidence."
            )
            return FakeStream(text)

        def create(self, _request):
            raise AssertionError("Worker calls with activity hooks should stream")

        def is_api_error(self, _exc):
            return False

        def is_retryable_error(self, _exc):
            return False

    monkeypatch.setattr(
        direct_agent,
        "get_provider",
        lambda *_args, **_kwargs: FakeProvider(),
    )
    monkeypatch.setattr(
        direct_agent,
        "_PROVIDER_ERROR_TEXT_RETRY_DELAYS",
        (0,),
    )

    result = await direct_agent.run_agent_async(
        message="Read the repository shard",
        model="claude-sonnet-4-6",
        tools=[],
        persist_session=False,
        session_id="spawn-worker-overload-retry",
        resolved_llm=_FakeLLM(),
        tool_call_source="worker",
        on_stream_activity=lambda _label: None,
        on_stream_delta=streamed_deltas.append,
        metadata={
            "execution_provenance": {
                "origin": "spawn_worker",
                "spawned_by_tool": True,
            }
        },
    )

    assert len(provider_calls) == 2
    assert result.success is True
    assert result.output == "Recovered repository evidence."
    assert raw_provider_error not in streamed_deltas
    assert streamed_deltas == ["Recovered repository evidence."]


@pytest.mark.parametrize(
    "failure_kind",
    ["transport_disconnect", "provider_server_error"],
)
async def test_interactive_slack_transient_failure_retries_stream_and_completes(
    monkeypatch,
    failure_kind,
):
    import httpx

    from brain.platform.integrations.openai_codex_client import (
        OpenAICodexRetryableError,
    )
    from brain.platform.integrations.provider_error_sentinel import (
        is_retryable_provider_error,
    )
    from brain.platform.integrations.providers import (
        is_transient_transport_disconnect,
    )
    from brain.systems.runs import direct_agent

    raw_error = (
        "peer closed connection without sending complete message body "
        "(incomplete chunked read)"
    )
    provider_calls = []

    class FakeStream:
        def __init__(self, attempt):
            self.attempt = attempt

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            if self.attempt == 1:
                yield SimpleNamespace(type="text", text="Partial answer")
                if failure_kind == "provider_server_error":
                    raise OpenAICodexRetryableError("server_error")
                raise httpx.RemoteProtocolError(raw_error)
            yield SimpleNamespace(type="text", text="Recovered answer")

        def get_final_message(self):
            return _response("Recovered answer")

    class FakeProvider:
        def stream(self, request):
            provider_calls.append(request)
            return FakeStream(len(provider_calls))

        def create(self, _request):
            raise AssertionError("Interactive calls should stream")

        def is_api_error(self, _exc):
            return False

        def is_retryable_error(self, exc):
            return is_transient_transport_disconnect(exc) or is_retryable_provider_error(
                exc
            )

    monkeypatch.setattr(direct_agent, "get_provider", lambda *_args, **_kwargs: FakeProvider())
    monkeypatch.setattr(direct_agent, "_API_RETRY_DELAYS", (0,))

    result = await direct_agent.run_agent_async(
        message="Continue the design conversation",
        model="claude-sonnet-4-6",
        tools=[],
        persist_session=False,
        session_id="interactive-transport-retry",
        resolved_llm=_FakeLLM(),
        on_stream_delta=lambda _delta: None,
        metadata={"execution_provenance": {"origin": "slack_teammate"}},
    )

    assert len(provider_calls) == 2
    assert result.success is True
    assert result.output == "Recovered answer"
    assert result.error is None


async def test_interactive_slack_transport_disconnect_retry_is_bounded(monkeypatch):
    import httpx

    from brain.platform.integrations.providers import is_transient_transport_disconnect
    from brain.systems.runs import direct_agent

    raw_error = (
        "peer closed connection without sending complete message body "
        "(incomplete chunked read)"
    )
    provider_calls = []

    class BrokenStream:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def __iter__(self):
            raise httpx.RemoteProtocolError(raw_error)
            yield  # pragma: no cover - make this a generator

        def get_final_message(self):
            raise AssertionError("A disconnected stream has no final message")

    class FakeProvider:
        def stream(self, request):
            provider_calls.append(request)
            return BrokenStream()

        def create(self, _request):
            raise AssertionError("Interactive calls should stream")

        def is_api_error(self, _exc):
            return False

        def is_retryable_error(self, exc):
            return is_transient_transport_disconnect(exc)

    monkeypatch.setattr(direct_agent, "get_provider", lambda *_args, **_kwargs: FakeProvider())
    monkeypatch.setattr(direct_agent, "_API_RETRY_DELAYS", (0,))

    result = await direct_agent.run_agent_async(
        message="Continue the design conversation",
        model="claude-sonnet-4-6",
        tools=[],
        persist_session=False,
        session_id="interactive-transport-exhausted",
        resolved_llm=_FakeLLM(),
        on_stream_delta=lambda _delta: None,
        metadata={"execution_provenance": {"origin": "slack_teammate"}},
    )

    assert len(provider_calls) == 2
    assert result.success is False
    assert result.output == ""
    assert result.error == raw_error


async def test_run_agent_async_basic_completion_uses_async_session_hooks(monkeypatch):
    from brain.systems.runs import direct_agent

    provider_calls = []
    hook_calls = []
    loop_id = id(asyncio.get_running_loop())
    handoff_started = asyncio.Event()
    handoff_release = asyncio.Event()
    handoff_completed = asyncio.Event()

    class FakeProvider:
        def create(self, request):
            provider_calls.append(request)
            return _response("async hello")

        def is_api_error(self, _exc):
            return False

        def is_retryable_error(self, _exc):
            return False

    async def load_session(session_id, user_id=None):
        hook_calls.append(("load_session", id(asyncio.get_running_loop()), session_id, user_id))
        return [], None

    async def load_session_handoff(session_id, user_id=None):
        hook_calls.append(("load_session_handoff", id(asyncio.get_running_loop()), session_id, user_id))
        return None

    async def save_session(session_id, messages, system_prompt, *token_args, user_id=None):
        hook_calls.append(("save_session", id(asyncio.get_running_loop()), session_id, len(messages), token_args, user_id))

    async def save_session_handoff(session_id, payload, *, user_id=None):
        handoff_started.set()
        await handoff_release.wait()
        hook_calls.append(("save_session_handoff", id(asyncio.get_running_loop()), session_id, payload, user_id))
        handoff_completed.set()

    monkeypatch.setattr(direct_agent, "get_provider", lambda *_args, **_kwargs: FakeProvider())

    run_agent_async = getattr(direct_agent, "run_agent_async")
    result = await asyncio.wait_for(
        run_agent_async(
            message="Say hello",
            model="claude-sonnet-4-6",
            tools=[],
            persist_session=True,
            session_id="async-session",
            resolved_llm=_FakeLLM(),
            load_session=load_session,
            load_session_handoff=load_session_handoff,
            save_session=save_session,
            save_session_handoff=save_session_handoff,
        ),
        timeout=1,
    )

    assert result.success
    assert result.output == "async hello"
    assert len(provider_calls) >= 1
    assert {call[1] for call in hook_calls} == {loop_id}
    assert [call[0] for call in hook_calls] == [
        "load_session",
        "load_session_handoff",
        "save_session",
    ]
    assert len(result.post_completion_tasks) == 1
    handoff_task = asyncio.create_task(result.post_completion_tasks[0]())
    await asyncio.wait_for(handoff_started.wait(), timeout=1)
    assert not handoff_completed.is_set()
    handoff_release.set()
    await asyncio.wait_for(handoff_completed.wait(), timeout=1)
    await handoff_task
    assert hook_calls[-1][0] == "save_session_handoff"


async def test_run_agent_async_honors_async_cancellation_without_sync_polling(monkeypatch):
    from brain.systems.runs import direct_agent

    class CancelToken:
        async def a_is_set(self):
            return True

        def is_set(self):
            raise AssertionError("native async agent cancellation must not use sync is_set()")

    class UnusedProvider:
        def create(self, _request):
            raise AssertionError("provider should not be called after async cancellation")

        def is_api_error(self, _exc):
            return False

        def is_retryable_error(self, _exc):
            return False

    monkeypatch.setattr(direct_agent, "get_provider", lambda *_args, **_kwargs: UnusedProvider())

    run_agent_async = getattr(direct_agent, "run_agent_async")
    result = await run_agent_async(
        message="Cancel me",
        model="claude-sonnet-4-6",
        tools=[],
        persist_session=False,
        session_id="async-cancel",
        resolved_llm=_FakeLLM(),
        cancel_event=CancelToken(),
    )

    assert not result.success
    assert result.error == "Cancelled by runner"


def test_sync_run_agent_edge_uses_async_auth_resolver(monkeypatch):
    from brain.systems.runs import direct_agent

    class FakeProvider:
        def create(self, _request):
            return _response("sync edge used async auth")

        def is_api_error(self, _exc):
            return False

        def is_retryable_error(self, _exc):
            return False

    async def async_resolve_llm_client(**_kwargs):
        return _FakeLLM()

    assert not hasattr(direct_agent, "resolve_llm_client")
    monkeypatch.setattr(direct_agent, "async_resolve_llm_client", async_resolve_llm_client)
    monkeypatch.setattr(direct_agent, "get_provider", lambda *_args, **_kwargs: FakeProvider())

    result = direct_agent.run_agent(
        message="Say hello",
        model="claude-sonnet-4-6",
        tools=[],
        persist_session=False,
        session_id="sync-edge",
        user_id="user-1",
        metadata={"org_id": "org-1"},
    )

    assert result.success
    assert result.output == "sync edge used async auth"
