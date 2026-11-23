"""A tool budget bounds execution and still leaves one request for an answer."""

import json

import httpx
import pytest
from mcp.types import Tool

from brain import loop
from brain.wire.chat import Chat
from tests.brain_service.test_chatgpt import collect, subscription
from tests.brain_service.test_review_responses import call, complete, message, stream
from tests.fakes import FakeMCPSession


@pytest.mark.parametrize("auth", ["api_key", "chatgpt"])
@pytest.mark.parametrize("batch", [False, True])
async def test_responses_forces_one_answer_without_extra_tools(run_request, monkeypatch, auth, batch):
    monkeypatch.setenv("OPENAI_API_KEY", "fake-api-key")
    request = subscription(run_request) if auth == "chatgpt" else run_request
    request.max_turns = 2
    bodies = []

    def respond(wire):
        body = json.loads(wire.content)
        bodies.append(body)
        assert body["instructions"] == request.system_prompt
        if body.get("tool_choice") == "none":
            assert body["tools"] == []
            assert any("Answer now" in str(i) for i in body["input"])
            results = [i for i in body["input"] if i.get("type") == "function_call_output"]
            assert len(results) == 3
            return stream(complete([message("I checked two lanes. I did not check the third.")]))
        calls = [call(i) for i in range(3)] if batch else [call(len(bodies))]
        return stream(complete(calls, usage={"total_tokens": 1}))

    events, session = await collect(request, respond)
    assert len(session.calls) == 2
    assert len([e for e in events if e["type"] == "tool_call"]) == 2
    assert len(bodies) == (2 if batch else 4)
    assert sum(b.get("tool_choice") == "none" for b in bodies) == 1
    assert events[-1]["type"] == "final" and events[-1]["usage"]["limited"] is True
    assert events[-1]["text"] == "I checked two lanes. I did not check the third."


def completion(calls=None, text=""):
    delta = {"tool_calls": [
        {"index": i, "id": f"call-{i}", "type": "function", "function": {
            "name": "lanes_search", "arguments": "{}"}}
        for i in range(calls)
    ]} if calls else {"content": text}
    return stream({"choices": [{"delta": delta,
                                 "finish_reason": "tool_calls" if calls else "stop"}]})


@pytest.mark.parametrize("batch", [False, True])
async def test_compat_counts_calls_and_forces_one_answer(run_request, batch):
    run_request.max_turns = 2
    bodies = []
    session = FakeMCPSession()

    def respond(wire):
        body = json.loads(wire.content)
        bodies.append(body)
        assert body["messages"][0]["content"] == run_request.system_prompt
        if body.get("tool_choice") == "none":
            assert not body.get("tools")
            assert "Answer now" in body["messages"][-1]["content"]
            assert len([m for m in body["messages"] if m["role"] == "tool"]) == 3
            return completion(text="I checked two lanes.")
        return completion(calls=3 if batch else 1)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        events = [e async for e in loop.conversation(
            Chat, run_request, session, [Tool(name="lanes_search", inputSchema={"type": "object"})], http
        )]
    assert len(session.calls) == 2
    assert len(bodies) == (2 if batch else 4)
    assert events[-1]["type"] == "final" and events[-1]["usage"]["limited"] is True


async def test_compat_exact_limit_keeps_the_normal_last_request(run_request):
    run_request.max_turns = 1
    bodies = []
    session = FakeMCPSession()

    def respond(wire):
        bodies.append(json.loads(wire.content))
        return completion(calls=1) if len(bodies) == 1 else completion(text="Ready.")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        events = [e async for e in loop.conversation(
            Chat, run_request, session, [Tool(name="lanes_search", inputSchema={"type": "object"})], http
        )]
    assert len(session.calls) == 1 and len(bodies) == 2
    assert bodies[-1].get("tools") and bodies[-1].get("tool_choice") != "none"
    assert events[-1] == {"type": "final", "text": "Ready.", "usage": {}}


@pytest.mark.parametrize("limited", [False, True])
async def test_self_looping_adapter_uses_the_loop_last_request(run_request, limited):
    """The loop, not the adapter, decides the last request, its prompt, its usage and its answer."""
    log = []

    async def steps():
        try:
            yield {"type": "started", "catalog": []}
            if limited:
                yield {"type": "error", "category": "limit", "usage": {"input_tokens": 2}}
                log.append("not closed")
            yield {"type": "final", "text": "Done", "usage": {"input_tokens": 2}}
        finally:
            log.append("closed")

    async def without_tools(prompt):
        log.append(prompt)
        yield {"type": "final", "text": "The release is ready.", "usage": {"input_tokens": 3}}

    transcript = [{"role": "UserMessage", "content": [{"text": "Lane: release"}]}]
    events = [e async for e in loop.self_looping(run_request, steps(), transcript, without_tools)]
    if not limited:
        assert log == ["closed"]
        assert events[-1] == {"type": "final", "text": "Done", "usage": {"input_tokens": 2}}
        return
    closed, prompt = log
    assert closed == "closed"
    assert prompt.startswith(run_request.user_message + "\n\n<outside_data source=\"transcript\">")
    assert "Lane: release" in prompt and prompt.endswith("\n\n" + loop.LIMIT_INSTRUCTION)
    assert [e["type"] for e in events] == ["started", "final"]
    assert events[-1]["text"] == "The release is ready."
    assert events[-1]["usage"] == {"input_tokens": 5, "limited": True}


@pytest.mark.parametrize("stage", ["steps", "last request"])
async def test_self_looping_closes_each_request_when_the_caller_closes(run_request, stage):
    """A cancelled run closes the SDK client at once, not later at garbage collection."""
    from contextlib import aclosing

    closed = []

    async def steps():
        try:
            yield {"type": "started", "catalog": []}
            yield {"type": "error", "category": "limit", "usage": {}}
        finally:
            closed.append("steps")

    async def without_tools(prompt):
        try:
            yield {"type": "started", "catalog": []}
            yield {"type": "final", "text": "Done", "usage": {}}
        finally:
            closed.append("last request")

    events = loop.self_looping(run_request, steps(), [], without_tools)
    async with aclosing(events):
        await anext(events)
        if stage == "last request":
            await anext(events)
    assert closed[-1] == stage
