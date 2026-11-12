"""Keep image data private and use the same content shape for both sign-ins."""

import base64
import json
import logging

import httpx
import pytest
from mcp.types import Tool
from pydantic import ValidationError

from brain.models import ChatGPTAuth, RunImage
from brain.runtimes import openai_responses
from brain.server import create_app
from illo.brain.client import RunImage as ClientImage
from illo.brain.client import RunRequest as ClientRequest
from tests.brain_service.test_openai_responses import FakeResponses
from tests.brain_service.test_service import lines, post
from tests.fakes import FakeMCPSession

DATA = base64.b64encode(b"\x89PNG\r\n\x1a\nAcme image test bytes").decode()


@pytest.mark.parametrize("subscription", [False, True])
async def test_responses_body_includes_image_for_both_sign_ins(
    run_request, monkeypatch, subscription
):
    monkeypatch.setenv("OPENAI_API_KEY", "test-api-key")
    run_request.runtime = "openai_responses"
    run_request.images = [RunImage(mime="image/png", data=DATA)]
    if subscription:
        run_request.provider_auth = ChatGPTAuth(
            access_token="test-token", account_id="acme-account"
        )
    provider = FakeResponses()
    provider.app.post("/backend-api/codex/responses")(provider.respond)

    async def respond(request):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=provider.app)) as client:
            return await client.send(request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        events = [
            e
            async for e in openai_responses.conversation(
                run_request,
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
        runtime="openai_responses",
        model="test",
        max_turns=1,
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
        ServiceRequest.model_validate({**request.wire(), "runtime": "bad"})
    assert DATA not in str(failure.value)


@pytest.mark.parametrize("runtime", ["claude", "openai_compat", "echo"])
def test_text_runtime_drops_images_with_a_notice(run_request, runtime):
    from brain.models import RunRequest

    request = RunRequest.model_validate(
        {**run_request.wire(), "runtime": runtime, "images": [{"mime": "image/png", "data": DATA}]}
    )
    assert request.images == []
    assert "The images could not be shown." in request.user_message


async def test_provider_image_echo_never_enters_service_logs(run_request, monkeypatch, caplog):
    monkeypatch.setenv("ILLO_BRAIN_SECRET", "service-test-secret")
    run_request.runtime = "openai_responses"
    run_request.images = [RunImage(mime="image/png", data=DATA)]
    caplog.set_level(logging.DEBUG)

    async def echo(request):
        yield {"type": "error", "message": DATA, "category": "runtime"}

    response = await post(create_app(runner=echo), run_request)
    assert DATA not in response.text
    assert DATA not in caplog.text
    assert lines(response)[-1]["message"] == "[redacted]"
    assert DATA not in json.dumps(lines(response))


async def test_image_data_in_tool_arguments_never_reaches_mcp(run_request, monkeypatch):
    from mcp.types import Tool

    from brain.models import RunError

    monkeypatch.setenv("OPENAI_API_KEY", "test-api-key")
    run_request.runtime = "openai_responses"
    run_request.images = [RunImage(mime="image/png", data=DATA)]
    provider = FakeResponses(arguments=json.dumps({"query": DATA}))
    session = FakeMCPSession()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=provider.app)) as client:
        with pytest.raises(RunError, match="private image data"):
            _ = [
                e
                async for e in openai_responses.conversation(
                    run_request,
                    session,
                    [Tool(name="lanes_search", inputSchema={"type": "object"})],
                    client,
                )
            ]
    assert not session.calls
