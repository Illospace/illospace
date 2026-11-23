"""Deep request options use the provider's Responses item format."""

import asyncio
import json

import httpx
import pytest
from mcp.types import CallToolResult, TextContent, Tool

from brain import loop
from brain.models import ChatGPTAuth, event
from brain.server import create_app
from brain.wire.responses import WEB_UNAVAILABLE, Responses, build_request
from tests.brain_service.conftest import CHATGPT, openai
from tests.brain_service.test_service import lines, post

SEARCH = {
    "type": "web_search_call",
    "id": "ws_1",
    "status": "completed",
    "action": {"type": "search", "query": "service retirement"},
}
ANNOTATIONS = [
    {
        "type": "url_citation",
        "start_index": 0,
        "end_index": 5,
        "title": "Source",
        "url": "https://example.com/proof",
    }
]
TOOLS = [Tool(name="lanes_search", inputSchema={"type": "object"})]


def final(text="Ready"):
    return {
        "type": "message",
        "id": "msg_1",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": ANNOTATIONS}],
    }


def stream(output):
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=(
            "data: "
            + json.dumps(
                {
                    "type": "response.completed",
                    "response": {"status": "completed", "output": output},
                }
            )
            + "\n\n"
        ),
    )


class Session:
    async def call_tool(self, *args):
        return CallToolResult(
            content=[TextContent(type="text", text="read result")],
            structuredContent={"run_flags": {"customer_read": False, "personal_read": False}},
        )


@pytest.mark.parametrize("auth", ["api_key", "chatgpt"])
async def test_search_body_and_stateless_replay(run_request, monkeypatch, auth):
    run_request.web_search = True
    run_request.effort = "xhigh"
    run_request.max_output_tokens = 16000
    provider = openai(api_key="fake-key")
    if auth == "chatgpt":
        run_request.provider, provider = "chatgpt", CHATGPT
        run_request.provider_auth = ChatGPTAuth(
            kind="chatgpt", access_token="token", account_id="account"
        )
    fallback = None
    bodies = []

    def respond(request):
        body = json.loads(request.content)
        bodies.append(body)
        assert {"type": "web_search"} in body["tools"]
        assert body["reasoning"] == {"effort": "xhigh"}
        assert ("max_output_tokens" in body) == (auth == "api_key")
        if len(bodies) == 1:
            return stream(
                [
                    SEARCH,
                    final("Source"),
                    {
                        "type": "function_call",
                        "id": "fc_1",
                        "call_id": "c1",
                        "name": "lanes_search",
                        "arguments": "{}",
                    },
                ]
            )
        return stream([final()])

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        events = [item async for item in loop.conversation(Responses, run_request, provider, Session(), TOOLS, client, fallback)]
    assert SEARCH in bodies[1]["input"]
    assert final("Source") in bodies[1]["input"]
    assert len([item for item in events if item["type"] == "web_search"]) == 1
    assert events[-1]["type"] == "final"
    assert build_request(run_request, TOOLS, [], tools_disabled=True)["tools"] == []


@pytest.mark.parametrize("auth", ["api_key", "chatgpt"])
async def test_provider_rejection_retries_once_without_search(run_request, monkeypatch, auth):
    run_request.web_search = True
    provider = openai(api_key="fake-key")
    if auth == "chatgpt":
        run_request.provider, provider = "chatgpt", CHATGPT
        run_request.provider_auth = ChatGPTAuth(
            kind="chatgpt", access_token="token", account_id="account"
        )
    fallback = None
    bodies = []

    def respond(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return httpx.Response(
                400,
                json={"error": {"param": "tools[0].type", "message": "web_search is unsupported"}},
            )
        return stream([final()])

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        events = [item async for item in loop.conversation(Responses, run_request, provider, Session(), TOOLS, client, fallback)]
    assert len(bodies) == 2
    assert {"type": "web_search"} not in bodies[1]["tools"]
    assert bodies[1]["input"][0]["content"].endswith(WEB_UNAVAILABLE)
    assert not run_request.web_search and events[-1]["type"] == "final"


async def test_one_cap_bounds_each_route_timeout(run_request, monkeypatch):
    monkeypatch.setenv("ILLO_BRAIN_SECRET", "service-test-secret")

    async def runner(request, *_):
        await asyncio.sleep(0.03)
        yield event("final", text="Ready")

    monkeypatch.setenv("ILLO_BRAIN_MAX_TIMEOUT_S", "0.01")
    app = create_app(runner=runner)
    run_request.timeout_s = 1200
    assert lines(await post(app, run_request))[-1]["category"] == "timeout"
    monkeypatch.setenv("ILLO_BRAIN_MAX_TIMEOUT_S", "1800")
    app = create_app(runner=runner)
    assert lines(await post(app, run_request, effort="xhigh"))[-1]["type"] == "final"
    assert lines(await post(app, run_request, timeout_s=0.01))[-1]["category"] == "timeout"
    # The deep flag is gone. A deep run is a route with its own limits.
    assert (await post(app, run_request, deep=True)).status_code == 422


async def test_subscription_fallback_keeps_search_ids(run_request, monkeypatch):
    run_request.provider, provider = "chatgpt", CHATGPT
    run_request.provider_auth = ChatGPTAuth(
        kind="chatgpt", access_token="subscription-token", account_id="account"
    )
    run_request.web_search = True
    run_request.fallback_provider = "openai"
    fallback = openai(api_key="fake-api-key")
    bodies = []

    def respond(request):
        body = json.loads(request.content)
        bodies.append(body)
        if len(bodies) == 1:
            return stream(
                [
                    SEARCH,
                    {
                        "type": "function_call",
                        "id": "fc1",
                        "call_id": "c1",
                        "name": "lanes_search",
                        "arguments": "{}",
                    },
                ]
            )
        if len(bodies) == 2:
            return httpx.Response(
                429, json={"error": {"type": "usage_limit_reached", "code": "usage_limit_reached"}}
            )
        assert request.headers["authorization"] == "Bearer fake-api-key"
        assert SEARCH in body["input"]
        return stream([final()])

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        events = [item async for item in loop.conversation(Responses, run_request, provider, Session(), TOOLS, client, fallback)]
    assert len(bodies) == 3
    assert events[-1]["usage"]["auth_kind"] == "api_key"
