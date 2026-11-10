"""Script a streaming model server and exercise the MCP loop over HTTP."""

import json

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from brain.runtimes import openai_compat
from tests.brain_service.conftest import serve


class FakeProvider:
    def __init__(self, *, loop=False, unknown=False, malformed=False):
        self.requests = []
        self.loop = loop
        self.unknown = unknown
        self.malformed = malformed
        self.app = FastAPI()
        self.app.post("/v1/chat/completions")(self.complete)

    async def complete(self, request: Request):
        body = await request.json()
        self.requests.append(body)

        async def stream():
            if (len(self.requests) == 1 or self.loop) and body.get("tool_choice") != "none":
                name = "shell" if self.unknown else "lanes_search"
                values = [
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": name, "arguments": "{"},
                            }
                        ]
                    },
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {
                                    "arguments": '"query":"release"}'
                                    if not self.malformed
                                    else "bad}"
                                },
                            }
                        ]
                    },
                ]
                finish = "tool_calls"
            else:
                values = [{"content": "Acme release "}, {"content": "is ready."}]
                finish = "stop"
            for delta in values:
                yield (
                    "data: "
                    + json.dumps({"choices": [{"index": 0, "delta": delta, "finish_reason": None}]})
                    + "\n\n"
                )
            yield (
                "data: "
                + json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
                + "\n\n"
            )
            yield (
                "data: "
                + json.dumps(
                    {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2}}
                )
                + "\n\n"
            )
            yield "data: [DONE]\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")


@pytest.mark.parametrize("tool_error,malformed", [(False, False), (True, False), (False, True)])
async def test_stream_tool_result_and_usage(
    run_request, fake_mcp, monkeypatch, tmp_path, tool_error, malformed
):
    fake_mcp.tool_error = tool_error
    fake_mcp.page = True
    provider = FakeProvider(malformed=malformed)
    async with serve(provider.app) as base:
        monkeypatch.setenv("OPENAI_BASE_URL", base + "/v1")
        run_request.mcp_url = fake_mcp.url
        events = [item async for item in openai_compat.run(run_request, tmp_path)]
    assert events[0]["catalog"] == ["asks_list", "lanes_search", "slack_post_status"]
    assert [item["name"] for item in events if item["type"] == "tool_call"] == ["lanes_search"]
    assert events[-1] == {
        "type": "final",
        "text": "Acme release is ready.",
        "usage": {"prompt_tokens": 20, "completion_tokens": 4},
    }
    assert [item["text"] for item in events if item["type"] == "text"] == [
        "Acme release ",
        "is ready.",
    ]
    assert len(fake_mcp.calls) == (0 if malformed else 1)
    output = provider.requests[1]["messages"][-1]["content"]
    assert output.startswith("Tool error.") == (tool_error or malformed)
    if not malformed:
        assert output.removeprefix("Tool error.\n") == "Acme release is ready"
    assert all(tool["type"] == "function" for tool in provider.requests[0]["tools"])
    assert "fake-run-token" not in json.dumps(provider.requests)


async def test_structured_payload_does_not_reach_the_model(run_request, fake_mcp, monkeypatch):
    fake_mcp.result_override = {
        "content": [{"type": "text", "text": "Acme release is ready"}],
        "structuredContent": {"result": "duplicate payload must stay out"},
    }
    provider = FakeProvider()
    async with serve(provider.app) as base:
        monkeypatch.setenv("OPENAI_BASE_URL", base + "/v1")
        run_request.mcp_url = fake_mcp.url
        events = [item async for item in openai_compat.run(run_request, None)]
    assert events[-1]["type"] == "final"
    assert provider.requests[1]["messages"][-1]["content"] == "Acme release is ready"
    assert "duplicate payload" not in json.dumps(provider.requests)


async def test_turn_limit(run_request, fake_mcp, monkeypatch, tmp_path):
    provider = FakeProvider(loop=True)
    async with serve(provider.app) as base:
        monkeypatch.setenv("OPENAI_BASE_URL", base + "/v1")
        run_request.mcp_url = fake_mcp.url
        run_request.max_turns = 2
        events = [item async for item in openai_compat.run(run_request, tmp_path)]
    assert len(provider.requests) == 4
    assert len(fake_mcp.calls) == 2
    assert events[-1]["type"] == "final"
    assert events[-1]["usage"]["limited"] is True
    assert provider.requests[-1]["tool_choice"] == "none"
    assert "tools" not in provider.requests[-1]


async def test_unknown_tool_cannot_execute(run_request, fake_mcp, monkeypatch, tmp_path):
    provider = FakeProvider(unknown=True)
    async with serve(provider.app) as base:
        monkeypatch.setenv("OPENAI_BASE_URL", base + "/v1")
        run_request.mcp_url = fake_mcp.url
        with pytest.raises(ExceptionGroup):
            _ = [item async for item in openai_compat.run(run_request, tmp_path)]
    assert fake_mcp.calls == []


async def test_provider_auth_error_survives_mcp_task_group(run_request, fake_mcp, monkeypatch):
    from fastapi.responses import JSONResponse

    from brain.worker import error_category

    provider = FastAPI()

    @provider.post("/v1/chat/completions")
    async def reject():
        return JSONResponse({"error": "private provider detail"}, status_code=401)

    async with serve(provider) as base:
        monkeypatch.setenv("OPENAI_BASE_URL", base + "/v1")
        run_request.mcp_url = fake_mcp.url
        with pytest.raises(ExceptionGroup) as error:
            _ = [item async for item in openai_compat.run(run_request, None)]
    assert error_category(error.value) == "auth"


def test_empty_base_url_means_the_default(monkeypatch):
    """Compose passes OPENAI_BASE_URL as an empty string when the operator sets none."""
    import inspect

    from brain.runtimes import openai_compat

    source = inspect.getsource(openai_compat.conversation)
    assert 'os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1"' in source


@pytest.mark.parametrize("effort", [None, "none"])
async def test_optional_reasoning_effort(run_request, fake_mcp, monkeypatch, effort):
    if effort is None:
        monkeypatch.delenv("ILLO_BRAIN_OPENAI_COMPAT_REASONING_EFFORT", raising=False)
    else:
        monkeypatch.setenv("ILLO_BRAIN_OPENAI_COMPAT_REASONING_EFFORT", effort)
    provider = FakeProvider()
    async with serve(provider.app) as base:
        monkeypatch.setenv("OPENAI_BASE_URL", base + "/v1")
        run_request.mcp_url = fake_mcp.url
        _ = [item async for item in openai_compat.run(run_request, None)]
    assert all(r.get("reasoning_effort") == effort for r in provider.requests)
    if effort is None:
        assert all("reasoning_effort" not in r for r in provider.requests)


async def test_safe_bad_request_event_and_log(run_request, fake_mcp, monkeypatch, caplog):
    from fastapi.responses import JSONResponse

    from brain.server import create_app
    from tests.brain_service.test_service import lines, post

    provider = FastAPI()

    @provider.post("/v1/chat/completions")
    async def reject():
        return JSONResponse(
            {
                "error": {
                    "code": "unsupported_parameter",
                    "type": "invalid_request_error",
                    "message": "PRIVATE PROMPT",
                }
            },
            status_code=400,
        )

    async with serve(provider) as base:
        monkeypatch.setenv("OPENAI_BASE_URL", base + "/v1")
        monkeypatch.setenv("ILLO_BRAIN_SECRET", "service-test-secret")
        run_request.mcp_url = fake_mcp.url
        run_request.runtime = "openai_compat"
        events = lines(await post(create_app(), run_request))
    assert events[-1]["code"] == "unsupported_parameter"
    assert events[-1]["provider_type"] == "invalid_request_error"
    assert events[-1]["http_status"] == 400
    assert "unsupported_parameter" in caplog.text
    assert "PRIVATE" not in json.dumps(events) + caplog.text
