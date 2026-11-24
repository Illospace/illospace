"""Exercise Gemini on the generic Chat wire with a fake HTTP transport."""

import copy
import json
from dataclasses import replace

import httpx
import pytest
from mcp.types import Tool

from brain import loop, providers
from brain.errors import RunError
from brain.models import RunImage
from brain.providers import BUILT_IN
from brain.wire.chat import Chat
from brain.wire.responses import Responses
from tests.brain_service.conftest import openai
from tests.brain_service.test_images import DATA
from tests.fakes import FakeMCPSession

USAGE = {"prompt_tokens": 10, "completion_tokens": 5, "completion_tokens_details": {"reasoning_tokens": 3}}
TOOL = Tool(name="lanes_search", inputSchema={"type": "object"})


def sse(*chunks):
    return httpx.Response(200, text="".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks))


def choice(field, part, stop=None):
    return {"choices": [{"index": 0, field: part, "finish_reason": stop}]}


def call(call_id="call_0", query="release"):
    return {
        "id": call_id, "type": "function",
        "function": {"name": TOOL.name, "arguments": json.dumps({"query": query})},
        "extra_content": {"google": {"thought_signature": "fake-signature"}},
    }


@pytest.fixture
def gemini(run_request):
    run_request.provider, run_request.effort = "gemini", "minimal"
    return providers.load({"ILLO_PROVIDER_GEMINI_API_KEY": "fake-gemini-key"})["gemini"]


async def collect(request, provider, respond, session=None, wire=Chat):
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        return [
            item async for item in loop.conversation(
                wire, request, provider, session or FakeMCPSession(), [TOOL], http
            )
        ]


@pytest.mark.parametrize("stream", [True, False])
async def test_assistant_message_is_replayed_with_all_fields(run_request, gemini, stream):
    gemini = replace(gemini, stream_tools=stream)
    calls = [call(), call("call_1", "schedule")]
    first = {
        "role": "assistant", "content": "First, I will check.",
        "extra_content": {"provider": {"opaque": "first", "counter": 2, "enabled": False}},
        "metadata": ["unchanged", 1], "refusal": None, "tool_calls": calls,
    }
    response = copy.deepcopy(first)
    bodies = []
    part = {
        **{key: value for key, value in first.items() if key != "tool_calls"},
        "content": "First, ",
        "extra_content": {"provider": {"opaque": "", "counter": 2}},
        "tool_calls": [
            {"index": 1, **calls[1]},
            {"index": 0, "id": "call_0", "type": "function",
             "function": {"name": "lanes_", "arguments": '{"query": '},
             "extra_content": {"google": {"thought_signature": ""}}},
        ],
    }
    tail = {
        "content": "I will check.",
        "extra_content": {"provider": {"opaque": "first", "enabled": False}},
        "tool_calls": [
            {"index": 0, "id": "call_0", "type": "",
             "function": {"name": "search", "arguments": '"release"}'},
             "extra_content": calls[0]["extra_content"]},
            {"index": 1, "id": "call_1", "extra_content": calls[1]["extra_content"]},
        ],
    }

    def respond(request):
        assert str(request.url) == "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
        assert request.headers["Authorization"] == "Bearer fake-gemini-key"
        body = json.loads(request.content)
        bodies.append(body)
        assert body["stream"] is stream
        assert ("stream_options" in body) is stream
        assert body["reasoning_effort"] == "minimal"
        if len(bodies) == 1:
            if stream:
                return sse(choice("delta", part), choice("delta", tail, "tool_calls"), {"usage": USAGE})
            return httpx.Response(200, json={**choice("message", response, "tool_calls"), "usage": USAGE})
        assert body["messages"][2] == first
        assert [message["tool_call_id"] for message in body["messages"][3:]] == ["call_0", "call_1"]
        message = {"role": "assistant", "content": "Ready."}
        if stream:
            return sse(choice("delta", message, "stop"), {"usage": USAGE})
        return httpx.Response(200, json={**choice("message", message, "stop"), "usage": USAGE})

    session = FakeMCPSession()
    events = await collect(run_request, gemini, respond, session)
    assert len(session.calls) == 2
    assert "".join(e["text"] for e in events if e["type"] == "text") == "First, I will check.Ready."
    assert events[-1]["usage"] == {
        "prompt_tokens": 20, "completion_tokens": 10, "completion_tokens_details": {"reasoning_tokens": 6},
    }


@pytest.mark.parametrize(("first", "later"), [
    ({"tool_calls": [{"index": 0, "id": "first"}]}, {"tool_calls": [{"index": 0, "id": "second"}]}),
    ({"tool_calls": [{"index": 0, "type": "function"}]}, {"tool_calls": [{"index": 0, "type": "other"}]}),
    ({"extra_content": {"nested": {"name": "first"}}}, {"extra_content": {"nested": {"name": "second"}}}),
    ({"tool_calls": [{"index": 0, "unknown": "first"}]},
     {"tool_calls": [{"index": 0, "unknown": "second"}]}),
])
async def test_conflicting_non_fragment_strings_fail_without_echo(run_request, gemini, first, later):
    with pytest.raises(RunError, match="conflicting message fields") as error:
        await collect(run_request, gemini, lambda _: sse(choice("delta", first), choice("delta", later)))
    assert error.value.category == "runtime"
    assert "first" not in str(error.value) and "second" not in str(error.value)


@pytest.mark.parametrize("refusal", [False, True])
async def test_chat_image_part_and_text_retry(run_request, gemini, refusal):
    run_request.images = [RunImage(mime="image/png", data=DATA)]
    bodies = []

    def respond(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            assert body["messages"][1]["content"] == [
                {"type": "text", "text": run_request.user_message},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64," + DATA}},
            ]
            if refusal:
                return httpx.Response(400, json={"error": {"status": "INVALID_ARGUMENT"}})
        else:
            assert body["messages"][1]["content"] == run_request.user_message + loop.IMAGES_REFUSED
        return sse(choice("delta", {"content": "Ready."}, "stop"))

    events = await collect(run_request, gemini, respond)
    assert len(bodies) == (2 if refusal else 1)
    assert events[-1]["text"] == "Ready."


@pytest.mark.parametrize("split", [True, False])
async def test_image_echo_never_reaches_mcp(run_request, gemini, split):
    run_request.images = [RunImage(mime="image/png", data=DATA)]
    session = FakeMCPSession()
    part = call(query=DATA)
    tail = []
    if split:
        args = part["function"]["arguments"]
        midpoint = len(args) // 2
        part["function"]["arguments"] = args[:midpoint]
        tail = [choice("delta", {"tool_calls": [{"index": 0, "function": {"arguments": args[midpoint:]}}]})]
    with pytest.raises(RunError, match="private image data"):
        await collect(run_request, gemini, lambda _: sse(
            choice("delta", {"tool_calls": [{"index": 0, **part}]}),
            *tail, choice("delta", {}, "tool_calls"),
        ), session)
    assert not session.calls


@pytest.mark.parametrize(("status", "body", "category"), [
    (429, {}, "limit"),
    (400, {"status": "RESOURCE_EXHAUSTED"}, "limit"),
    (400, {"status": "INVALID_ARGUMENT", "details": [{"reason": "API_KEY_INVALID"}]}, "config"),
    (400, {"status": "INVALID_ARGUMENT", "param": "api_key"}, "config"),
    (400, {"status": "INVALID_ARGUMENT"}, "runtime"),
    (401, {}, "config"),
    (403, {}, "config"),
    (500, {}, "transient"),
    (503, {}, "transient"),
    (400, {"code": "rate_limit_exceeded"}, "runtime"),
])
async def test_chat_errors_use_structured_status(run_request, gemini, monkeypatch, status, body, category):
    sleeps = []

    async def sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(loop, "sleep", sleep)
    with pytest.raises(RunError) as error:
        await collect(run_request, gemini, lambda _: httpx.Response(
            status, json={"error": {**body, "message": "PRIVATE INVALID KEY RESOURCE_EXHAUSTED"}},
        ))
    assert error.value.category == category
    assert error.value.details["http_status"] == status
    assert "PRIVATE" not in str(error.value)
    assert sleeps == ([2] if category == "transient" else [])


@pytest.mark.parametrize("after", ["none", "text", "tool"])
@pytest.mark.parametrize("wire", [Chat, Responses])
async def test_transient_retry_depends_on_output_in_this_request(run_request, gemini, monkeypatch, after, wire):
    sleeps, requests = [], []
    session = FakeMCPSession()
    provider = gemini if wire is Chat else openai()
    run_request.provider = provider.name

    async def sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(loop, "sleep", sleep)

    def respond(request):
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            if wire is Chat:
                return sse(choice("delta", {"tool_calls": [{"index": 0, **call()}]}, "tool_calls"))
            return sse({"type": "response.completed", "response": {"status": "completed", "output": [
                {"type": "function_call", "call_id": "first", "name": TOOL.name, "arguments": "{}"},
            ]}})
        if len(requests) == 2:
            if after == "none":
                return httpx.Response(503)
            if wire is Chat:
                output = {"content": "partial"} if after == "text" else {"tool_calls": [{"index": 0, **call()}]}
                return sse(choice("delta", output), {"error": {"code": 503, "status": "UNAVAILABLE"}})
            output = ({"type": "response.output_text.delta", "delta": "partial"} if after == "text"
                      else {"type": "response.output_item.added", "item": {
                          "type": "function_call", "name": TOOL.name,
                      }})
            return sse(output, {"type": "error", "code": "server_error"})
        if wire is Chat:
            return sse(choice("delta", {"content": "Ready."}, "stop"))
        return sse({"type": "response.completed", "response": {"status": "completed", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": "Ready."}]},
        ]}})

    if after == "none":
        events = await collect(run_request, provider, respond, session, wire)
        assert events[-1]["text"] == "Ready." and sleeps == [2]
        assert requests[1] == requests[2]
    else:
        with pytest.raises(RunError) as error:
            await collect(run_request, provider, respond, session, wire)
        assert error.value.category == "transient"
        assert len(requests) == 2 and not sleeps
    assert len(session.calls) == 1


async def test_list_wrapped_error_body_maps_key_error_without_image_retry(run_request, gemini):
    run_request.images = [RunImage(mime="image/png", data=DATA)]
    calls = 0

    def respond(_):
        nonlocal calls
        calls += 1
        return httpx.Response(400, json=[{"error": {
            "code": 400, "status": "INVALID_ARGUMENT",
            "details": [{"reason": "API_KEY_INVALID"}],
        }}])

    with pytest.raises(RunError) as error:
        await collect(run_request, gemini, respond)
    assert error.value.category == "config"
    assert calls == 1


async def test_stream_joins_reasoning_fragments(run_request, gemini):
    bodies = []

    def respond(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return sse(
                choice("delta", {"role": "assistant", "reasoning_content": "First ", "reasoning": "a "}),
                choice("delta", {"reasoning_content": "step.", "reasoning": "b."}),
                choice("delta", {"tool_calls": [{"index": 0, **call()}]}, "tool_calls"),
            )
        return sse(choice("delta", {"content": "Ready."}, "stop"))

    events = await collect(run_request, gemini, respond, FakeMCPSession())
    assert events[-1]["text"] == "Ready."
    assistant = bodies[1]["messages"][2]
    assert assistant["reasoning_content"] == "First step."
    assert assistant["reasoning"] == "a b."


async def test_transient_fallback_failure_keeps_subscription_limit(run_request, monkeypatch):
    from brain.models import ChatGPTAuth

    sleeps, calls = [], 0

    async def sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(loop, "sleep", sleep)
    run_request.provider = "chatgpt"
    run_request.provider_auth = ChatGPTAuth(access_token="fake-access", account_id="acct")
    run_request.fallback_provider = "openai"
    primary = BUILT_IN["chatgpt"]
    fallback = openai()

    def respond(_):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, json={"error": {"type": "usage_limit_reached"}})
        return httpx.Response(503)

    with pytest.raises(RunError) as error:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            async for _ in loop.conversation(
                Responses, run_request, primary, FakeMCPSession(), [TOOL], http, fallback,
            ):
                pass
    assert error.value.category == "limit"
    assert error.value.details["fallback_failed"] is True
    assert sleeps == [2]
    assert calls == 3

