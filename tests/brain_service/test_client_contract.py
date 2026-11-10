"""The foundation client and the service must agree on auth and events."""

import pytest

from brain.server import create_app
from illo.brain.client import BrainClient, RunRequest
from tests.brain_service.conftest import serve


async def test_foundation_client_echo_contract(fake_mcp, monkeypatch):
    monkeypatch.setenv("ILLO_BRAIN_SECRET", "acme-service-secret")
    request = RunRequest(
        run_id=42,
        system_prompt="Acme",
        user_message="List the tools",
        mcp_url=fake_mcp.url,
        mcp_token="fake-run-token",
        runtime="echo",
        model="none",
        max_turns=1,
        timeout_s=5,
    )
    async with serve(create_app()) as base:
        client = BrainClient(base)
        events = [item async for item in client.run(request)]
    assert events[0].type == "started"
    assert events[-1].type == "final"
    assert events[-1].run_id == 42
    assert events[-1].usage == {}
    assert "lanes_search" in events[-1].text


async def test_foundation_client_wrong_secret(fake_mcp, monkeypatch):
    import httpx

    monkeypatch.setenv("ILLO_BRAIN_SECRET", "acme-service-secret")
    request = RunRequest(
        run_id=42,
        system_prompt="Acme",
        user_message="List the tools",
        mcp_url=fake_mcp.url,
        mcp_token="fake-run-token",
        runtime="echo",
        model="none",
        max_turns=1,
        timeout_s=5,
    )
    async with serve(create_app()) as base:
        with pytest.raises(httpx.HTTPStatusError) as error:
            _ = [item async for item in BrainClient(base, secret="wrong").run(request)]
    assert error.value.response.status_code == 401


async def test_real_gateway_mcp_catalog(app, rpc_token, monkeypatch):
    import json

    monkeypatch.setenv("ILLO_BRAIN_SECRET", "acme-service-secret")
    run, token = rpc_token
    expected = [tool["name"] for tool in app.state.ctx.registry.list_tools(run)]
    async with serve(app) as gateway_base, serve(create_app()) as brain_base:
        request = RunRequest(
            run_id=run["id"],
            system_prompt="Acme",
            user_message="List tools",
            mcp_url=gateway_base + "/brain/mcp",
            mcp_token=token,
            runtime="echo",
            model="none",
            max_turns=1,
            timeout_s=5,
        )
        events = [item async for item in BrainClient(brain_base).run(request)]
    assert events[-1].type == "final"
    assert json.loads(events[-1].text) == expected


@pytest.mark.parametrize(
    "category,code,expected",
    [
        ("runtime", "unsupported_parameter", "runtime unsupported_parameter"),
        ("auth", "invalid_api_key", "auth invalid_api_key"),
        ("timeout", "unknown", "timeout unknown"),
        ("runtime", "PRIVATE PROMPT", "runtime unknown"),
    ],
)
async def test_run_manager_keeps_safe_failure(app, replay, category, code, expected):
    from illo.brain.client import BrainEvent

    class FailedBrain:
        async def run(self, request):
            yield BrainEvent(
                type="error", category=category, code=code, message="PRIVATE PROVIDER MESSAGE"
            )

    app.state.ctx.brain_client = FailedBrain()
    await replay()
    async with app.state.ctx.pool.connection() as conn:
        row = await (await conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1")).fetchone()
    assert row["error"] == "The brain run failed: " + expected
    assert row["status"] == ("timeout" if category == "timeout" else "failed")
    assert row["token_hash"] is None
    assert "PRIVATE" not in row["error"]


def test_responses_workspace_and_env_override(monkeypatch):
    from illo.config import Config
    from illo.workspace import BrainConfig

    assert BrainConfig(runtime="openai_responses").runtime == "openai_responses"
    monkeypatch.setenv("ILLO_BRAIN_RUNTIME", "openai_responses")
    assert Config.from_env().brain_runtime == "openai_responses"


def test_s1_default_wire_accepts_a_brain_without_smoke():
    from pydantic import create_model

    from brain.models import RunRequest as ServiceRequest

    request = RunRequest(
        run_id=1, system_prompt="Acme", user_message="Hello", mcp_url="http://test/brain/mcp",
        mcp_token="fake", runtime="openai_responses", model="test", max_turns=1, timeout_s=5,
    )
    fields = {
        name: (field.annotation, field)
        for name, field in ServiceRequest.model_fields.items() if name != "smoke"
    }
    old_request = create_model("OldRequest", __config__=ServiceRequest.model_config, **fields)
    assert old_request.model_validate(request.wire()).run_id == 1
    request.smoke = True
    assert request.wire()["smoke"] is True
