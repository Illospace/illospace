"""Run the smoke CLI through the real run manager and a fake model transport."""

import json
import shutil
from typing import get_args

import httpx
import pytest
from psycopg.conninfo import make_conninfo

from brain import loop, providers
from brain.errors import failure_event
from brain.server import create_app
from brain.wire.chat import Chat
from illo.brain import smoke
from illo.brain.client import BrainClient
from illo.cli import execute, parser
from illo.db import one
from illo.workspace import RunKind
from tests.brain_service.test_gemini_chat import USAGE, call, choice, sse
from tests.conftest import ROOT


def test_smoke_parser_accepts_each_run_kind_and_removes_chatgpt_smoke():
    for kind in get_args(RunKind):
        args = parser().parse_args([
            "brain", "smoke", "--route", kind, "--as", "ada",
            "--provider", "gemini", "--model", "gemini-4-neon",
        ])
        assert (args.route, args.person_key, args.provider, args.model) == (
            kind, "ada", "gemini", "gemini-4-neon",
        )
    with pytest.raises(SystemExit):
        parser().parse_args(["chatgpt", "smoke", "--person", "ada"])


@pytest.mark.parametrize("outcome", ["pass", "batch", "wrong_token", "wrong_color", "image_refused"])
async def test_gemini_smoke_requires_dependent_rounds_and_the_image(
    app, monkeypatch, tmp_path, capsys, outcome,
):
    ctx = app.state.ctx
    root = tmp_path / "workspace"
    shutil.copytree(ROOT / "workspace.example", root)
    ctx.config.workspace_dir = root
    async with ctx.pool.connection() as conn:
        ctx.config.database_url = make_conninfo(**conn.info.get_parameters(), password=conn.info.password)
    monkeypatch.setattr(smoke.secrets, "choice", lambda _: "red")
    monkeypatch.setenv("ILLO_BRAIN_SECRET", "fake-brain-secret")
    table = providers.load({"ILLO_PROVIDER_GEMINI_API_KEY": "fake-gemini-key"})
    sent, challenges, run_ids = [], set(), []

    def respond(wire):
        body = json.loads(wire.content)
        sent.append(body)
        assert body["model"] == "gemini-4-neon"
        assert [item["function"]["name"] for item in body["tools"]] == ["smoke_step"]
        message = body["messages"][1]["content"]
        if outcome == "image_refused" and len(sent) == 1:
            return httpx.Response(400, json={"error": {"status": "INVALID_ARGUMENT"}})
        if outcome != "image_refused":
            assert message[0]["text"] == smoke.SmokeCheck.question
            assert message[1]["image_url"]["url"].startswith("data:image/png;base64,")
        else:
            assert message.endswith(loop.IMAGES_REFUSED)
        prior = [item["content"] for item in body["messages"] if item["role"] == "tool"]
        challenges.update(prior)
        step = len(prior) + 1
        if step == 4:
            answer = "blue" if outcome == "wrong_color" else "red"
            return sse(choice("delta", {"content": answer}, "stop"), {"usage": USAGE})
        args = {"n": step}
        if prior:
            args["token"] = "incorrect" if outcome == "wrong_token" else prior[-1]
        tool_call = {"index": 0, **call(f"call_{step}")}
        tool_call["function"] = {"name": "smoke_step", "arguments": json.dumps(args)}
        tool_calls = [tool_call]
        if outcome == "batch":
            tool_calls.append({**tool_call, "id": "call_2", "index": 1})
        return sse(choice("delta", {
            "role": "assistant", "extra_content": {"opaque": "fake-private-value"},
            "tool_calls": tool_calls,
        }, "tool_calls"), {"usage": USAGE})

    async def runner(request, selected, fallback):
        run_ids.append(request.run_id)
        assert request.smoke and request.provider_auth.kind == "api_key"
        async with ctx.pool.connection() as conn:
            row = await one(conn, "SELECT status, token_hash FROM runs WHERE id=%s", (request.run_id,))
        assert row["status"] == "running" and row["token_hash"]
        try:
            async for item in loop.run(Chat, request, selected, tmp_path, transport=httpx.MockTransport(respond)):
                yield item
        except Exception as error:
            yield failure_event(error)

    service = create_app(runner=runner, table=table)
    client = BrainClient("http://brain", transport=httpx.ASGITransport(app=service))
    monkeypatch.setattr(smoke, "BrainClient", lambda _: client)
    args = ["brain", "smoke", "--route", "person_dm", "--provider", "gemini", "--model", "gemini-4-neon"]
    assert await execute(parser().parse_args(args), ctx.config) == (0 if outcome == "pass" else 1)
    report = capsys.readouterr().out
    assert '"type": "response"' in report and '"stop": "tool_calls"' in report
    assert "tool_calls.extra_content.google.thought_signature" in report
    assert '"reasoning_tokens": 3' in report
    for value in ("fake-gemini-key", "fake-signature", "fake-private-value", *challenges):
        assert value not in report
    if outcome == "pass":
        assert len(sent) == 4
        assert report.count('"type": "tool_call"') == 3
        assert "The smoke check passed" in report and '"stop": "stop"' in report and '"reasoning_tokens": 12' in report
    async with ctx.pool.connection() as conn:
        row = await one(conn, "SELECT * FROM runs")
    assert run_ids == [row["id"]]
    assert row["route"] == "person_dm" and row["provider"] == "gemini" and row["model"] == "gemini-4-neon"
    assert row["payer"] == "api_key" and row["token_hash"] is None
    assert row["usage"]["smoke"] is True
    assert row["status"] == ("completed" if outcome == "pass" else "failed")
    assert smoke.load_workspace(root).config.brain.routes["default"].provider == "chatgpt"
