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
            if len(self.requests) == 1 or self.loop:
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
    assert json.loads(provider.requests[1]["messages"][-1]["content"])["isError"] == (
        tool_error or malformed
    )
    assert all(tool["type"] == "function" for tool in provider.requests[0]["tools"])
    assert "fake-run-token" not in json.dumps(provider.requests)


async def test_turn_limit(run_request, fake_mcp, monkeypatch, tmp_path):
    provider = FakeProvider(loop=True)
    async with serve(provider.app) as base:
        monkeypatch.setenv("OPENAI_BASE_URL", base + "/v1")
        run_request.mcp_url = fake_mcp.url
        run_request.max_turns = 2
        events = [item async for item in openai_compat.run(run_request, tmp_path)]
    assert len(provider.requests) == 2
    assert len(fake_mcp.calls) == 2
    assert events[-1]["category"] == "limit"
    assert not any(item["type"] == "final" for item in events)


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
