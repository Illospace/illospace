"""The subscription transport shares the exact Responses tool boundary."""

import json

import httpx
import pytest
from mcp.types import Tool

from brain.models import ChatGPTAuth, RunError
from brain.runtimes import openai_responses
from brain.server import create_app
from tests.brain_service.test_openai_responses import FakeResponses
from tests.brain_service.test_service import lines, post
from tests.fakes import FakeMCPSession

MARKER = "fake-chatgpt-token-marker"


def subscription(request):
    request.runtime = "openai_responses"
    request.provider_auth = ChatGPTAuth(access_token=MARKER, account_id="acme-account")
    return request


async def collect(request, handler, names=("lanes_search",)):
    tools = [Tool(name=n, inputSchema={"type": "object"}) for n in names]
    session = FakeMCPSession()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = [e async for e in openai_responses.conversation(request, session, tools, client)]
    return events, session


async def test_chatgpt_request_and_function_round_trip(run_request, monkeypatch):
    request = subscription(run_request)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://wrong.example/v1")
    provider = FakeResponses()
    provider.app.post("/backend-api/codex/responses")(provider.respond)
    headers = []

    async def respond(wire):
        headers.append(wire.headers)
        assert str(wire.url) == "https://chatgpt.com/backend-api/codex/responses"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=provider.app)) as client:
            return await client.send(wire)

    events, session = await collect(request, respond)
    assert events[-1]["type"] == "final" and events[-1]["text"] == "Release ready."
    assert len(session.calls) == 1
    for body, head in zip(provider.requests, headers, strict=True):
        assert head["authorization"] == "Bearer " + MARKER
        assert head["chatgpt-account-id"] == "acme-account"
        assert head["originator"] == "illo-brain"
        assert body["instructions"] == request.system_prompt
        assert body["store"] is False and body["stream"] is True
        assert "max_output_tokens" not in body and "previous_response_id" not in body
        assert body["include"] == ["reasoning.encrypted_content"]
        assert body["tool_choice"] == "auto"
        assert all(t["type"] == "function" and t["name"] == "lanes_search" for t in body["tools"])
        assert MARKER not in json.dumps(body)
    output = provider.requests[1]["input"][-1]
    assert output["type"] == "function_call_output" and output["call_id"] == "call_0"


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("fallback", [False, True])
async def test_limit_and_api_fallback_resume_tool_results(
    run_request, monkeypatch, fallback, stream
):
    request = subscription(run_request)
    request.api_key_fallback = fallback
    monkeypatch.setenv("OPENAI_API_KEY", "fake-api-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.example/v1")
    wires = []

    def respond(wire):
        wires.append(wire)
        body = json.loads(wire.content)
        first = len(wires) == 1
        if first:
            output = [
                {
                    "type": "function_call",
                    "name": "lanes_search",
                    "call_id": "call-1",
                    "arguments": "{}",
                }
            ]
        elif wire.url.host == "chatgpt.com":
            error = {"type": "usage_limit_reached", "resets_at": 1893456000, "message": MARKER}
            if not stream:
                return httpx.Response(429, json={"error": error})
            return httpx.Response(
                200,
                text="data: "
                + json.dumps({"type": "response.failed", "response": {"error": error}})
                + "\n\n",
            )
        else:
            assert wire.headers["authorization"] == "Bearer fake-api-key"
            assert "chatgpt-account-id" not in wire.headers and "originator" not in wire.headers
            assert any(i.get("type") == "function_call_output" for i in body["input"])
            output = [{"type": "message", "content": [{"type": "output_text", "text": "Ready."}]}]
        return httpx.Response(
            200,
            text="data: "
            + json.dumps(
                {
                    "type": "response.completed",
                    "response": {"status": "completed", "output": output},
                }
            )
            + "\n\n",
        )

    if not fallback:
        with pytest.raises(RunError) as caught:
            await collect(request, respond)
        assert str(caught.value) == "Your ChatGPT limit is reached. It resets 2030-01-01 00:00 UTC."
        assert caught.value.details["resets_at"] == 1893456000
        assert len(wires) == 2
    else:
        events, session = await collect(request, respond)
        assert events[-1]["text"].endswith("I used the API key for this request.\n\nReady.")
        assert len(session.calls) == 1 and len(wires) == 3


async def test_api_retry_is_once_and_never_silent(run_request, monkeypatch):
    request = subscription(run_request)
    request.api_key_fallback = True
    monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
    count = 0

    def respond(wire):
        nonlocal count
        count += 1
        return httpx.Response(
            429, json={"error": {"type": "usage_limit_reached", "code": "rate_limit_exceeded"}}
        )

    with pytest.raises(RunError, match="Your ChatGPT limit is reached. The API fallback failed."):
        await collect(request, respond)
    assert count == 2


async def test_missing_api_key_still_reports_subscription_limit(run_request, monkeypatch):
    request = subscription(run_request)
    request.api_key_fallback = True
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    count = 0

    def respond(wire):
        nonlocal count
        count += 1
        return httpx.Response(429, json={"error": {"type": "usage_limit_reached"}})

    with pytest.raises(
        RunError, match="Your ChatGPT limit is reached. The API fallback failed."
    ) as caught:
        await collect(request, respond)
    assert count == 1 and caught.value.details["api_key_fallback_failed"]


async def test_unknown_chatgpt_tools_refused(run_request):
    request = subscription(run_request)
    calls = []

    def respond(wire):
        calls.append(wire)
        return httpx.Response(
            200,
            text='data: {"type":"response.output_item.added","item":{"type":"web_search_call"}}\n\n',
        )

    with pytest.raises(RunError, match="outside the run catalog"):
        await collect(request, respond)
    assert len(calls) == 1


async def test_secret_wire_and_service_redaction(run_request, monkeypatch, caplog):
    request = subscription(run_request)
    monkeypatch.setenv("ILLO_BRAIN_SECRET", "fake-service-secret")
    assert MARKER not in repr(request) + request.model_dump_json()
    assert request.wire()["provider_auth"]["access_token"] == MARKER

    async def runner(request):
        yield {
            "type": "error",
            "category": "runtime",
            "code": MARKER,
            "message": MARKER,
            "usage": {MARKER: 1},
        }

    response = await post(create_app(runner=runner), request, secret="fake-service-secret")
    assert lines(response)[-1]["type"] == "error"
    assert MARKER not in response.text + caplog.text
    response = await post(create_app(), request, secret="fake-service-secret", runtime="claude")
    assert response.status_code == 422 and MARKER not in response.text


async def test_token_echo_in_tool_arguments_cannot_reach_mcp(run_request):
    request = subscription(run_request)
    session = FakeMCPSession()

    def respond(wire):
        chunk = {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "name": "lanes_search",
                        "call_id": "call-1",
                        "arguments": json.dumps({"query": MARKER}),
                    }
                ],
            },
        }
        return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(RunError, match="The provider returned a credential"):
            _ = [
                e
                async for e in openai_responses.conversation(
                    request,
                    session,
                    [Tool(name="lanes_search", inputSchema={"type": "object"})],
                    client,
                )
            ]
    assert session.calls == []
