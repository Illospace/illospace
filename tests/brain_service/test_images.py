"""Keep image data private and use the same content shape for both sign-ins."""

import base64
import json
import logging

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from mcp.types import Tool
from pydantic import ValidationError

from brain import loop
from brain.models import ChatGPTAuth, RunImage
from brain.server import create_app
from brain.wire.responses import Responses
from illo.brain.client import RunImage as ClientImage
from illo.brain.client import RunRequest as ClientRequest
from tests.brain_service.conftest import CHATGPT, openai
from tests.brain_service.test_openai_responses import FakeResponses
from tests.brain_service.test_service import lines, post
from tests.fakes import FakeMCPSession

DATA = base64.b64encode(b"\x89PNG\r\n\x1a\nAcme image test bytes").decode()


@pytest.mark.parametrize("subscription", [False, True])
async def test_responses_body_includes_image_for_both_sign_ins(
    run_request, monkeypatch, subscription
):
    run_request.provider, selected = "openai", openai(api_key="test-api-key")
    run_request.images = [RunImage(mime="image/png", data=DATA)]
    if subscription:
        run_request.provider, selected = "chatgpt", CHATGPT
        run_request.provider_auth = ChatGPTAuth(
            access_token="test-token", account_id="acme-account"
        )
    provider = FakeResponses()
    provider.app.post("/backend-api/codex/responses")(provider.respond)

    async def respond(request, *_):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=provider.app)) as client:
            return await client.send(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        events = [
            e
            async for e in loop.conversation(
                Responses,
                run_request,
                selected,
                FakeMCPSession(),
                [Tool(name="lanes_search", inputSchema={"type": "object"})],
                client,
            )
        ]
    assert events[-1]["type"] == "final"
    for body in provider.requests:
        assert body["input"][0] == {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "Find the release"},
                {"type": "input_image", "image_url": "data:image/png;base64," + DATA},
            ],
        }
        assert body["store"] is False
        assert body["stream"] is True


def test_images_cross_both_wire_boundaries_but_not_normal_dumps(run_request):
    from brain.models import RunRequest as ServiceRequest

    request = ClientRequest(
        run_id=1,
        system_prompt="Acme",
        user_message="Read this image",
        mcp_url="http://test/brain/mcp",
        mcp_token="test",
        provider="openai",
        model="test",
        max_tool_calls=1,
        timeout_s=5,
        images=[ClientImage(mime="image/png", data=DATA)],
    )
    service = ServiceRequest.model_validate(request.wire())
    assert service.images[0].data.get_secret_value() == DATA
    assert service.wire()["images"] == [{"mime": "image/png", "data": DATA}]
    for value in (request, service, request.images[0], service.images[0]):
        assert DATA not in repr(value)
        assert DATA not in value.model_dump_json()
    with pytest.raises(ValidationError) as failure:
        ServiceRequest.model_validate({**request.wire(), "provider": "Not A Name"})
    assert DATA not in str(failure.value)


async def test_a_provider_without_images_is_a_config_error(run_request, monkeypatch):
    from brain.models import RunRequest

    monkeypatch.setenv("ILLO_BRAIN_SECRET", "service-test-secret")
    from brain.providers import BUILT_IN

    called = []

    async def runner(request, *_):
        called.append(request)
        yield {"type": "final", "text": "no", "usage": {}}

    request = RunRequest.model_validate(
        {**run_request.wire(), "provider": "anthropic", "effort": None,
         "images": [{"mime": "image/png", "data": DATA}]}
    )
    table = {"anthropic": BUILT_IN["anthropic"]}
    response = await post(create_app(runner=runner, table=table), request)
    assert lines(response)[-1]["category"] == "config" and not called
    assert DATA not in response.text


async def test_provider_image_echo_never_enters_service_logs(run_request, monkeypatch, caplog):
    monkeypatch.setenv("ILLO_BRAIN_SECRET", "service-test-secret")
    run_request.provider = "openai"
    run_request.images = [RunImage(mime="image/png", data=DATA)]
    caplog.set_level(logging.DEBUG)

    async def echo(request, *_):
        yield {"type": "error", "message": DATA, "category": "runtime"}

    response = await post(create_app(runner=echo, table={"openai": openai()}), run_request)
    assert DATA not in response.text
    assert DATA not in caplog.text
    assert lines(response)[-1]["message"] == "[redacted]"
    assert DATA not in json.dumps(lines(response))


async def test_image_data_in_tool_arguments_never_reaches_mcp(run_request, monkeypatch):
    from mcp.types import Tool

    from brain.errors import RunError

    run_request.provider, selected = "openai", openai(api_key="test-api-key")
    run_request.images = [RunImage(mime="image/png", data=DATA)]
    provider = FakeResponses(arguments=json.dumps({"query": DATA}))
    session = FakeMCPSession()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=provider.app)) as client:
        with pytest.raises(RunError, match="private image data"):
            _ = [
                e
                async for e in loop.conversation(
                    Responses,
                    run_request,
                    selected,
                    session,
                    [Tool(name="lanes_search", inputSchema={"type": "object"})],
                    client,
                )
            ]
    assert not session.calls


async def test_provider_refusal_of_images_retries_with_text_only(run_request, monkeypatch):
    run_request.provider, selected = "openai", openai(api_key="test-api-key")
    run_request.images = [RunImage(mime="image/png", data=DATA)]
    provider = FakeResponses()
    answer = provider.respond

    async def refuse_images(request: Request):
        body = await request.json()
        if "input_image" in json.dumps(body["input"]):
            provider.requests.append(body)
            return JSONResponse({"error": {"code": "invalid_image", "message": "x"}}, status_code=400)
        return await answer(request)

    provider.app = FastAPI()
    provider.app.post("/v1/responses")(refuse_images)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=provider.app), base_url="https://api.openai.com"
    ) as client:
        events = [
            e
            async for e in loop.conversation(
                Responses,
                run_request,
                selected,
                FakeMCPSession(),
                [Tool(name="lanes_search", inputSchema={"type": "object"})],
                client,
            )
        ]
    assert events[-1]["type"] == "final"
    assert "input_image" in json.dumps(provider.requests[0]["input"])
    for body in provider.requests[1:]:
        assert body["input"][0]["content"] == "Find the release" + loop.IMAGES_REFUSED
