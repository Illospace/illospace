"""Responses regression cases use an in-memory provider and fake tools."""

import json

import httpx

from tests.brain_service.test_chatgpt import collect, subscription


def stream(*chunks):
    return httpx.Response(200, text="".join("data: " + json.dumps(c) + "\n\n" for c in chunks))


def complete(output, **fields):
    return {
        "type": "response.completed",
        "response": {"status": "completed", "output": output, **fields},
    }


def call(index=0, arguments="{}"):
    return {
        "type": "function_call",
        "name": "lanes_search",
        "call_id": f"call-{index}",
        "arguments": arguments,
    }


def message(text="Ready."):
    return {"type": "message", "content": [{"type": "output_text", "text": text}]}


async def test_u05_fallback_drops_reasoning_and_failed_text(run_request, monkeypatch):
    request = subscription(run_request)
    request.api_key_fallback = True
    monkeypatch.setenv("OPENAI_API_KEY", "fake-api-key")
    bodies = []

    def respond(wire):
        body = json.loads(wire.content)
        bodies.append(body)
        if len(bodies) == 1:
            return stream(
                complete(
                    [{"type": "reasoning", "encrypted_content": "fake-chatgpt-reasoning"}, call()]
                )
            )
        if len(bodies) == 2:
            return stream(
                {"type": "response.output_text.delta", "delta": "Ready."},
                {"type": "response.failed", "response": {"error": {"type": "usage_limit_reached"}}},
            )
        return stream(
            {"type": "response.output_text.delta", "delta": "Ready."}, complete([message()])
        )

    events, session = await collect(request, respond)
    assert not any(i.get("type") == "reasoning" for i in bodies[-1]["input"])
    assert any(i.get("type") == "function_call_output" for i in bodies[-1]["input"])
    assert [e["text"] for e in events if e["type"] == "text"] == ["Ready."]
    assert len(session.calls) == 1


async def test_u08_parallel_calls_stop_at_limit(run_request):
    run_request = subscription(run_request)
    run_request.max_turns = 2
    events, session = await collect(
        run_request, lambda wire: stream(complete([call(i) for i in range(3)]))
    )
    assert len(session.calls) == 2
    assert len([e for e in events if e["type"] == "tool_call"]) == 2
    assert events[-1]["type"] == "error" and events[-1]["category"] == "limit"


async def test_u09_empty_arguments_call_tool(run_request):
    count = 0

    def respond(wire):
        nonlocal count
        count += 1
        return stream(complete([call(arguments="  ")] if count == 1 else [message()]))

    events, session = await collect(subscription(run_request), respond)
    assert session.calls == [{"name": "lanes_search", "arguments": {}}]
    assert events[-1]["text"] == "Ready."


async def test_u09_refusal_has_text(run_request):
    refusal = {
        "type": "message",
        "content": [{"type": "refusal", "refusal": "I cannot help with that."}],
    }
    events, _ = await collect(subscription(run_request), lambda wire: stream(complete([refusal])))
    assert events[-1]["text"] == "I cannot help with that."


async def test_u12_done_items_work_without_completed_output(run_request):
    count = 0

    def respond(wire):
        nonlocal count
        count += 1
        item = call() if count == 1 else message()
        return stream(
            {"type": "response.output_item.done", "output_index": 0, "item": item},
            {"type": "response.completed", "response": {"status": "completed", "usage": {"total_tokens": 2}}},
        )

    events, session = await collect(subscription(run_request), respond)
    assert len(session.calls) == 1
    assert events[-1]["text"] == "Ready." and events[-1]["usage"]["total_tokens"] == 4


async def test_s2_exact_tool_limit_gets_a_normal_last_model_call(run_request):
    request = subscription(run_request)
    request.max_turns = 1
    bodies = []

    def respond(wire):
        bodies.append(json.loads(wire.content))
        return stream(complete([call()] if len(bodies) == 1 else [message()]))

    events, session = await collect(request, respond)
    assert len(session.calls) == 1 and len(bodies) == 2
    assert events[-1]["type"] == "final" and events[-1]["text"] == "Ready."
    assert bodies[-1]["tools"] and bodies[-1].get("tool_choice") != "none"
