"""Script Responses events and check the stateless MCP loop."""

import json

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from brain.runtimes import openai_responses
from tests.brain_service.conftest import serve
from tests.brain_service.test_service import lines, post


class FakeResponses:
    def __init__(
        self, *, names=None, loop=False, status=None, retry=False, terminal=None, arguments=None
    ):
        self.requests = []
        self.names = names or ["lanes_search"]
        self.loop = loop
        self.status = status
        self.retry = retry
        self.terminal = terminal
        self.arguments = arguments
        self.app = FastAPI()
        self.app.post("/v1/responses")(self.respond)

    async def respond(self, request: Request):
        body = await request.json()
        self.requests.append(body)
        if self.status or (self.retry and "reasoning" in body):
            return JSONResponse(
                {
                    "error": {
                        "code": "unsupported_parameter" if self.retry else "model_not_found",
                        "type": "invalid_request_error",
                        "param": "reasoning.effort" if self.retry else "model",
                        "message": "PRIVATE PROMPT AND PROVIDER MESSAGE",
                    }
                },
                status_code=self.status or 400,
            )
        first = not any(item.get("type") == "function_call_output" for item in body["input"])
        output = [
            {
                "type": "reasoning",
                "id": "rs_1",
                "summary": [],
                "encrypted_content": "opaque-reasoning",
            }
        ]
        if (first or self.loop) and body.get("tool_choice") != "none":
            for index, name in enumerate(self.names):
                output.append(
                    {
                        "type": "function_call",
                        "id": f"fc_{index}",
                        "call_id": f"call_{index}",
                        "name": "lanes_search" if name == "builtin" else name,
                        "arguments": self.arguments
                        if self.arguments is not None
                        else '{"query":"release"}',
                    }
                )
        else:
            output.append(
                {
                    "type": "message",
                    "id": "msg_1",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Release ready."}],
                }
            )
        if self.names == ["builtin"]:
            output.append({"type": "web_search_call", "id": "ws_1"})

        async def stream():
            events = []
            if self.terminal:
                events.append(self.terminal)
            else:
                for index, item in enumerate(output):
                    events.append(
                        {"type": "response.output_item.added", "output_index": index, "item": item}
                    )
                if not first and not self.loop:
                    for delta in ("Release ", "ready."):
                        events.append({"type": "response.output_text.delta", "delta": delta})
                events.append(
                    {
                        "type": "response.completed",
                        "response": {
                            "id": "resp_1",
                            "status": "completed",
                            "output": output,
                            "usage": {
                                "input_tokens": 10,
                                "output_tokens": 5,
                                "total_tokens": 15,
                                "output_tokens_details": {"reasoning_tokens": 2},
                            },
                        },
                    }
                )
            for item in events:
                yield "event: " + item["type"] + "\ndata: " + json.dumps(item) + "\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")


async def collect(provider, request, fake_mcp, monkeypatch):
    from brain.server import create_app

    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai-key")
    monkeypatch.setenv("ILLO_BRAIN_SECRET", "service-test-secret")
    request.runtime = "openai_responses"
    request.mcp_url = fake_mcp.url
    async with serve(provider.app) as base:
        monkeypatch.setenv("OPENAI_BASE_URL", base + "/v1")
        response = await post(create_app(), request)
    return lines(response)


@pytest.mark.parametrize(
    "names,tool_error",
    [(["lanes_search"], False), (["lanes_search", "asks_list"], False), (["lanes_search"], True)],
)
async def test_function_loop(run_request, fake_mcp, monkeypatch, names, tool_error):
    fake_mcp.tool_error = tool_error
    fake_mcp.page = True
    provider = FakeResponses(names=names)
    events = await collect(provider, run_request, fake_mcp, monkeypatch)
    assert events[-1] == {
        "type": "final",
        "run_id": run_request.run_id,
        "text": "Release ready.",
        "usage": {
            "input_tokens": 20,
            "output_tokens": 10,
            "total_tokens": 30,
            "output_tokens_details": {"reasoning_tokens": 4},
        },
    }
    assert [e["name"] for e in events if e["type"] == "tool_call"] == names
    assert [c["name"] for c in fake_mcp.calls] == names
    assert [e["text"] for e in events if e["type"] == "text"] == ["Release ", "ready."]
    payload = provider.requests[1]
    assert payload["store"] is False and "previous_response_id" not in payload
    assert payload["instructions"] == run_request.system_prompt
    assert payload["max_output_tokens"] == 4096 and payload["parallel_tool_calls"]
    assert payload["reasoning"] == {"effort": "low"}
    assert payload["input"][1]["encrypted_content"] == "opaque-reasoning"
    results = [i for i in payload["input"] if i.get("type") == "function_call_output"]
    assert [i["call_id"] for i in results] == [f"call_{i}" for i in range(len(names))]
    assert all(i["output"].startswith("Tool error.") == tool_error for i in results)
    assert all(i["output"].removeprefix("Tool error.\n") == "Acme release is ready" for i in results)
    assert "fake-run-token" not in json.dumps(provider.requests)


async def test_structured_payload_does_not_reach_the_model(run_request, fake_mcp, monkeypatch):
    fake_mcp.result_override = {
        "content": [{"type": "text", "text": "Acme release is ready"}],
        "structuredContent": {"result": "duplicate payload must stay out"},
    }
    provider = FakeResponses()
    events = await collect(provider, run_request, fake_mcp, monkeypatch)
    assert events[-1]["type"] == "final"
    results = [i for i in provider.requests[1]["input"] if i.get("type") == "function_call_output"]
    assert [i["output"] for i in results] == ["Acme release is ready"]
    assert "duplicate payload" not in json.dumps(provider.requests)


@pytest.mark.parametrize("names", [["shell"], ["lanes_search", "shell"], ["builtin"]])
async def test_unknown_and_builtin_refused(run_request, fake_mcp, monkeypatch, names):
    provider = FakeResponses(names=names)
    events = await collect(provider, run_request, fake_mcp, monkeypatch)
    assert events[-1]["category"] == "runtime"
    assert events[-1]["type"] == "error"
    assert fake_mcp.calls == []


async def test_turn_limit(run_request, fake_mcp, monkeypatch):
    run_request.max_turns = 2
    provider = FakeResponses(loop=True)
    events = await collect(provider, run_request, fake_mcp, monkeypatch)
    assert events[-1]["type"] == "final" and events[-1]["usage"]["limited"] is True
    assert len(provider.requests) == 4 and len(fake_mcp.calls) == 2
    assert provider.requests[-1]["tools"] == []
    assert provider.requests[-1]["tool_choice"] == "none"


@pytest.mark.parametrize("status,category", [(401, "auth"), (400, "runtime")])
async def test_safe_http_error(run_request, fake_mcp, monkeypatch, caplog, status, category):
    provider = FakeResponses(status=status)
    events = await collect(provider, run_request, fake_mcp, monkeypatch)
    assert events[-1]["category"] == category
    assert events[-1]["http_status"] == status
    assert events[-1]["code"] == "model_not_found"
    assert events[-1]["provider_type"] == "invalid_request_error"
    assert "PRIVATE" not in json.dumps(events) + caplog.text
    assert "model_not_found" in caplog.text and str(status) in caplog.text
    assert len(provider.requests) == 1


async def test_effort_retry_and_process_cache(run_request, fake_mcp, monkeypatch, tmp_path):
    from brain.worker import failure_event

    provider = FakeResponses(retry=True)
    monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
    run_request.mcp_url = fake_mcp.url
    async with serve(provider.app) as base:
        monkeypatch.setenv("OPENAI_BASE_URL", base + "/v1")
        for _ in range(2):
            try:
                events = [e async for e in openai_responses.run(run_request, tmp_path)]
            except Exception as error:
                pytest.fail(str(failure_event(error)))
            assert events[-1]["type"] == "final"
    assert len(provider.requests) == 5
    assert "reasoning" in provider.requests[0]
    assert all("reasoning" not in r for r in provider.requests[1:])


@pytest.mark.parametrize(
    "terminal,category",
    [
        ({"type": "error", "code": "server_error", "message": "PRIVATE"}, "runtime"),
        (
            {
                "type": "response.failed",
                "response": {"error": {"code": "server_error", "message": "PRIVATE"}},
            },
            "runtime",
        ),
        (
            {
                "type": "response.incomplete",
                "response": {"incomplete_details": {"reason": "max_output_tokens"}},
            },
            "limit",
        ),
    ],
)
async def test_stream_errors(run_request, fake_mcp, monkeypatch, terminal, category):
    events = await collect(FakeResponses(terminal=terminal), run_request, fake_mcp, monkeypatch)
    assert events[-1]["category"] == category
    assert "PRIVATE" not in json.dumps(events)


@pytest.mark.parametrize("arguments", ["{bad}", "[]"])
async def test_invalid_arguments_return_tool_error(run_request, fake_mcp, monkeypatch, arguments):
    provider = FakeResponses(arguments=arguments)
    events = await collect(provider, run_request, fake_mcp, monkeypatch)
    assert events[-1]["type"] == "final"
    assert fake_mcp.calls == []
    assert provider.requests[-1]["input"][-1]["output"].startswith("Tool error.\n")


async def test_retry_stops_after_second_rejection(run_request, fake_mcp, monkeypatch):
    provider = FakeResponses(retry=True, status=400)
    events = await collect(provider, run_request, fake_mcp, monkeypatch)
    assert len(provider.requests) == 2
    assert events[-1]["type"] == "error"
    assert events[-1]["code"] == "unsupported_parameter"
    assert fake_mcp.calls == []


async def test_truncated_stream_fails(run_request, fake_mcp, monkeypatch):
    provider = FakeResponses(terminal={"type": "response.created", "response": {"id": "resp_1"}})
    events = await collect(provider, run_request, fake_mcp, monkeypatch)
    assert events[-1]["type"] == "error"
    assert events[-1]["category"] == "runtime"
    assert fake_mcp.calls == []


@pytest.mark.parametrize("base", [None, ""])
async def test_default_base_and_effort(run_request, monkeypatch, base):
    import httpx

    monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
    if base is None:
        monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("OPENAI_BASE_URL", base)
    monkeypatch.setenv("ILLO_BRAIN_EFFORT", "")
    received = []

    def respond(request):
        received.append(request)
        terminal = {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": "Ready."}]}
                ],
            },
        }
        return httpx.Response(200, text="data: " + json.dumps(terminal) + "\n\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        events = [
            item async for item in openai_responses.conversation(run_request, None, [], client)
        ]
    assert events[-1]["text"] == "Ready."
    assert str(received[0].url) == "https://api.openai.com/v1/responses"
    assert json.loads(received[0].content)["reasoning"] == {"effort": "low"}
