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
