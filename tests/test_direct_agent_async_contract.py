from __future__ import annotations

import asyncio
from types import SimpleNamespace


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
