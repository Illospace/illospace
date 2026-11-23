"""Pin the inputs that the two copies of the wire contract accept today.

These tests were recorded on the code before the loop refactor (#971). A change of an
accepted payload, a failure place or a wire value is a behavior change. It belongs in a
later slice, with a change of these tests. S2 (#972) replaced the runtime with a provider:
an unknown provider name is a `config` event of the provider table, not a 422.
"""

import httpx
import pytest
from pydantic import ValidationError

from brain.server import create_app
from illo.brain.client import BrainClient, ProviderAuth, RunRequest
from tests.brain_service.conftest import CHATGPT, openai

SECRET = "fake-wire-secret"
FIELDS = {
    "run_id": 42,
    "system_prompt": "Acme",
    "user_message": "Find the release",
    "mcp_url": "http://illo.example/brain/mcp",
    "mcp_token": "fake-run-token",
    "provider": "openai",
    "model": "fake-model",
    "max_tool_calls": 3,
    "timeout_s": 5,
}


@pytest.fixture(autouse=True)
def service_secret(monkeypatch):
    monkeypatch.setenv("ILLO_BRAIN_SECRET", SECRET)


def recording_service():
    """A service whose runner records each request that reaches it."""
    started = []

    async def runner(body, *_):
        started.append(body)
        yield {"type": "final", "text": "Done", "usage": {}}

    return create_app(runner=runner, table={"openai": openai(), "chatgpt": CHATGPT}), started


async def post(app, payload):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://brain"
    ) as http:
        return await http.post("/run", json=payload, headers={"X-Illo-Brain-Secret": SECRET})


async def test_service_selects_the_api_key_when_provider_auth_is_absent():
    app, started = recording_service()
    response = await post(app, dict(FIELDS))
    assert response.status_code == 200
    assert [body.provider_auth.kind for body in started] == ["api_key"]


@pytest.mark.parametrize(
    "auth",
    [
        pytest.param({}, id="empty-object"),
        pytest.param({"kind": "api_key", "access_token": None, "account_id": None},
                     id="api-key-with-null-tokens"),
        pytest.param({"kind": "chatgpt", "access_token": None, "account_id": None},
                     id="chatgpt-with-null-tokens"),
        pytest.param({"kind": "chatgpt", "access_token": None, "account_id": "fake-account"},
                     id="chatgpt-with-null-access-token"),
        pytest.param({"kind": "chatgpt"}, id="chatgpt-without-tokens"),
        pytest.param({"kind": "api_key", "access_token": "fake-token"}, id="api-key-with-token"),
    ],
)
async def test_service_refuses_these_sign_in_payloads_before_the_runner(auth):
    app, started = recording_service()
    response = await post(app, {**FIELDS, "provider_auth": auth})
    assert response.status_code == 422
    assert started == []


async def test_service_gives_an_empty_chatgpt_token_to_the_runner():
    """The provider, not the service, rejects an empty token. That gives an `auth` event."""
    app, started = recording_service()
    auth = {"kind": "chatgpt", "access_token": "", "account_id": ""}
    response = await post(app, {**FIELDS, "provider": "chatgpt", "provider_auth": auth})
    assert response.status_code == 200
    assert len(started) == 1
    assert started[0].provider_auth.kind == "chatgpt"
    assert started[0].provider_auth.access_token.get_secret_value() == ""


def test_client_refuses_an_empty_chatgpt_token():
    with pytest.raises(ValidationError):
        ProviderAuth(kind="chatgpt", access_token="", account_id="fake-account")


def test_client_sends_a_string_run_id_as_an_integer():
    wire = RunRequest(**{**FIELDS, "run_id": "42"}).wire()
    assert wire["run_id"] == 42 and type(wire["run_id"]) is int


@pytest.mark.parametrize("name", ["provider", "model", "max_tool_calls", "timeout_s"])
def test_client_needs_each_run_setting(name):
    fields = {key: value for key, value in FIELDS.items() if key != name}
    with pytest.raises(ValidationError):
        RunRequest(**fields)


async def test_unknown_provider_fails_at_the_service_not_in_the_client():
    request = RunRequest(**{**FIELDS, "provider": "acme"})
    assert request.wire()["provider"] == "acme"
    app, started = recording_service()
    client = BrainClient("http://brain", SECRET, transport=httpx.ASGITransport(app=app))
    events = [item async for item in client.run(request)]
    assert events[-1].type == "error" and events[-1].category == "config"
    request = RunRequest(**{**FIELDS, "provider": "Not A Name"})
    with pytest.raises(httpx.HTTPStatusError) as error:
        _ = [item async for item in client.run(request)]
    assert error.value.response.status_code == 422
    assert started == []
