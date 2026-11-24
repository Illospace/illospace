"""The unified smoke command keeps the normal sign-in and fallback policy."""

import json
import shutil

import httpx
import pytest
import yaml
from psycopg.conninfo import make_conninfo

from brain import loop
from brain.errors import failure_event
from brain.providers import load
from brain.wire.responses import Responses
from illo.brain import smoke
from illo.brain.client import BrainClient
from illo.cli import execute, parser
from illo.db import one
from tests.chatgpt.conftest import save_grant
from tests.conftest import ROOT


@pytest.mark.parametrize(
    "mode", ["own", "team", "no_as", "missing", "limit", "fallback", "provider_error", "no_tool"]
)
async def test_smoke_uses_normal_grant_refresh_and_fallback(
    chatgpt_app, monkeypatch, tmp_path, capsys, mode,
):
    app, token_backend = chatgpt_app
    ctx = app.state.ctx
    if mode != "missing":
        await save_grant(ctx, expires=0, access="fake-old-token")
    root = tmp_path / "workspace"
    shutil.copytree(ROOT / "workspace.example", root)
    path = root / "illo.yaml"
    data = yaml.safe_load(path.read_text())
    data["brain"]["chatgpt"].update(
        fallback="openai" if mode == "fallback" else "none",
        person_credential="team" if mode == "team" else "own",
    )
    path.write_text(yaml.safe_dump(data))
    ctx.config.workspace_dir = root
    async with ctx.pool.connection() as conn:
        ctx.config.database_url = make_conninfo(**conn.info.get_parameters(), password=conn.info.password)
    monkeypatch.setattr(smoke, "ChatGPTClient", lambda: ctx.chatgpt_client)
    monkeypatch.setattr(smoke.secrets, "choice", lambda _: "red")
    monkeypatch.setenv("ILLO_BRAIN_SECRET", "fake-brain-secret")

    from brain.server import create_app

    requests, brain_wires, tokens = [], [], []

    def respond(wire):
        body = json.loads(wire.content)
        requests.append(wire)
        assert [tool["name"] for tool in body["tools"]] == ["smoke_step"]
        if mode in {"limit", "fallback"} and wire.url.host == "chatgpt.com":
            return httpx.Response(
                429, json={"error": {"type": "usage_limit_reached", "message": "fake-provider-secret"}},
            )
        if mode == "provider_error":
            return httpx.Response(
                401, json={"error": {"code": "invalid_token", "message": "fake-provider-secret"}},
            )
        previous = [item["output"] for item in body["input"] if item.get("type") == "function_call_output"]
        tokens.extend(previous)
        args = {"n": len(previous) + 1}
        if previous:
            args["token"] = previous[-1]
        output = (
            {"type": "function_call", "name": "smoke_step", "call_id": f"step_{len(previous) + 1}",
             "arguments": json.dumps(args)}
            if len(previous) < 3 and mode != "no_tool"
            else {"type": "message", "content": [{"type": "output_text", "text": "Red"}]}
        )
        response = {"type": "response.completed", "response": {
            "status": "completed", "output": [output], "usage": {"input_tokens": 2, "output_tokens": 1},
        }}
        return httpx.Response(200, text="data: " + json.dumps(response) + "\n\n")

    async def runner(request, selected, fallback):
        brain_wires.append(request.wire())
        try:
            async for item in loop.run(Responses, request, selected, tmp_path, fallback=fallback,
                                       transport=httpx.MockTransport(respond)):
                yield item
        except Exception as error:
            yield failure_event(error)

    table = load({"ILLO_PROVIDER_OPENAI_API_KEY": "fake-api-secret"})
    service = create_app(runner=runner, table=table)
    client = BrainClient("http://brain", transport=httpx.ASGITransport(app=service))
    monkeypatch.setattr(smoke, "BrainClient", lambda _: client)
    args = ["brain", "smoke", "--route", "person_dm"]
    if mode not in {"team", "no_as"}:
        args += ["--as", "ada"]
    result = await execute(parser().parse_args(args), ctx.config)
    output = capsys.readouterr().out
    passed = mode in {"own", "team", "fallback"}
    assert result == (0 if passed else 1)
    for secret in (
        "fake-old-token", "fresh-access", "rotated-refresh", "account-ada", "fake-api-secret",
        "fake-provider-secret", *tokens,
    ):
        assert secret not in output
    async with ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM runs")
    assert row["route"] == "person_dm" and row["usage"]["smoke"] is True
    assert row["token_hash"] is None and row["status"] == ("completed" if passed else "failed")
    if mode in {"missing", "no_as"}:
        assert not brain_wires and not requests and "No model sign-in" in output
        assert token_backend.refresh_count == 0
        async with ctx.pool.connection() as conn:
            sensor = await one(conn, "SELECT 1 FROM sensors WHERE last_error_at IS NOT NULL LIMIT 1")
        assert sensor is None
    else:
        assert brain_wires[0]["provider_auth"]["access_token"] == "fresh-access"
        assert brain_wires[0]["smoke"] is True and brain_wires[0]["run_id"] == row["id"]
        assert brain_wires[0]["provider_auth"]["kind"] == "chatgpt"
        assert token_backend.refresh_count >= 1
        if passed:
            assert output.count('"type": "tool_call"') == 3
            assert '"input_tokens": 8' in output
            assert row["payer"] == "chatgpt"
            assert row["usage"]["auth_kind"] == ("api_key" if mode == "fallback" else "chatgpt")
        elif mode in {"limit", "provider_error"}:
            assert f'"http_status": {429 if mode == "limit" else 401}' in output
        else:
            assert "did not complete three dependent tool rounds" in output
