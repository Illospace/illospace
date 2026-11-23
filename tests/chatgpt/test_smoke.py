"""The smoke command sends fake credentials through the real brain HTTP contract."""

import json
import shutil

import httpx
import pytest
import yaml
from psycopg.conninfo import make_conninfo

from brain.models import RunRequest as ServiceRequest
from brain.runtimes import openai_responses
from brain.server import create_app
from brain.worker import failure_event
from illo.brain.client import BrainClient
from illo.cli import execute, parser
from illo.connectors import chatgpt_smoke
from tests.chatgpt.conftest import save_grant
from tests.conftest import ROOT


def test_smoke_parser():
    args = parser().parse_args(["chatgpt", "smoke", "--person", "ada"])
    assert args.person == "ada" and args.action == "smoke"


@pytest.mark.parametrize(
    "mode", ["success", "missing", "limit", "fallback", "provider_error", "no_tool", "explicit_api", "team"]
)
async def test_smoke_uses_refresh_and_brain_wire(chatgpt_app, monkeypatch, tmp_path, capsys, mode):
    app, token_backend = chatgpt_app
    ctx = app.state.ctx
    if mode != "missing":
        await save_grant(ctx, expires=0, access="fake-old-token")
    root = tmp_path / "workspace"
    shutil.copytree(ROOT / "workspace.example", root)
    path = root / "illo.yaml"
    data = yaml.safe_load(path.read_text())
    data["brain"].update(
        auth="chatgpt" if mode in {"explicit_api", "team"} else "api_key",
        runtime="openai_responses",
        team_credential="ada",
        model="test-model",
        api_key_fallback=mode == "fallback",
        person_credential="team" if mode == "team" else "own",
    )
    path.write_text(yaml.safe_dump(data))
    ctx.config.workspace_dir = root
    async with ctx.pool.connection() as conn:
        ctx.config.database_url = make_conninfo(
            **conn.info.get_parameters(), password=conn.info.password
        )
    monkeypatch.setattr(chatgpt_smoke, "ChatGPTClient", lambda: ctx.chatgpt_client)
    monkeypatch.setenv("OPENAI_API_KEY", "fake-api-secret")
    monkeypatch.setenv("ILLO_BRAIN_SECRET", "fake-brain-secret")
    requests = []
    brain_wires = []

    def respond(wire):
        body = json.loads(wire.content)
        requests.append(wire)
        assert len(body["tools"]) == 1 and body["tools"][0]["name"] == "smoke_ping"
        if mode in {"limit", "fallback"} and wire.url.host == "chatgpt.com":
            return httpx.Response(
                429,
                json={"error": {"type": "usage_limit_reached", "message": "fake-provider-secret"}},
            )
        if mode == "provider_error":
            return httpx.Response(
                401, json={"error": {"code": "invalid_token", "message": "fake-provider-secret"}}
            )
        first = not any(i.get("type") == "function_call_output" for i in body["input"])
        output = (
            [
                {
                    "type": "function_call",
                    "name": "smoke_ping",
                    "call_id": "fake-call",
                    "arguments": "{}",
                }
            ]
            if first and mode != "no_tool"
            else [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "fake-provider-secret"}],
                }
            ]
        )
        chunk = {
            "type": "response.completed",
            "response": {
                "status": "completed",
                "output": output,
                "usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
            },
        }
        return httpx.Response(200, text="data: " + json.dumps(chunk) + "\n\n")

    async def runner(request):
        assert isinstance(request, ServiceRequest)
        brain_wires.append(request.wire())
        try:
            async for item in openai_responses.run(
                request, tmp_path, transport=httpx.MockTransport(respond)
            ):
                yield item
        except Exception as error:
            yield failure_event(error)

    service = create_app(runner=runner)
    client = BrainClient(
        "http://brain", "fake-brain-secret", transport=httpx.ASGITransport(app=service)
    )
    monkeypatch.setattr(chatgpt_smoke, "BrainClient", lambda url: client)
    args = ["chatgpt", "smoke", "--person", "ben" if mode == "team" else "ada"]
    if mode == "explicit_api":
        args += ["--auth", "api_key"]
    result = await execute(parser().parse_args(args), ctx.config)
    output = capsys.readouterr().out
    assert result == (0 if mode in {"success", "fallback", "explicit_api", "team"} else 1)
    assert "Model: test-model" in output and "Latency: " in output and "Token usage: " in output
    for secret in (
        "fake-old-token",
        "fresh-access",
        "rotated-refresh",
        "account-ada",
        "fake-api-secret",
        "fake-provider-secret",
    ):
        assert secret not in output
    if mode == "missing":
        assert not brain_wires and not requests and "fallback is off" in output
        assert token_backend.refresh_count == 0
    else:
        assert token_backend.refresh_count == (0 if mode == "explicit_api" else 1)
        if mode != "explicit_api":
            assert brain_wires[0]["provider_auth"]["access_token"] == "fresh-access"
        assert brain_wires[0]["provider_auth"]["kind"] == (
            "api_key" if mode == "explicit_api" else "chatgpt"
        )
        assert brain_wires[0]["smoke"] is True
        if mode in {"success", "fallback", "explicit_api", "team"}:
            assert "Tool round trip: yes" in output and "total_tokens=6" in output
            assert f"Auth kind: {'api_key' if mode in {'fallback', 'explicit_api'} else 'chatgpt'}" in output
            assert any(
                i.get("type") == "function_call_output"
                for i in json.loads(requests[-1].content)["input"]
            )
        else:
            assert "Tool round trip: no" in output
            expected_status = 429 if mode == "limit" else "none" if mode == "no_tool" else 401
            assert f"HTTP {expected_status}" in output
            assert all(w.url.host == "chatgpt.com" for w in requests)
