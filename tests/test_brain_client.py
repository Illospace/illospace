"""Verify the brain HTTP wire contract without a model service."""

import json

import httpx

from illo.brain.client import BrainClient, RunRequest


async def test_http_brain_contract_uses_json_lines(monkeypatch):
    received = []

    def respond(request):
        assert request.url.path == "/run"
        received.append(json.loads(request.content))
        return httpx.Response(
            200,
            content='{"type":"started"}\n{"type":"text","text":"Working"}\n{"type":"final","text":"Done","usage":{"input_tokens":3}}\n',
        )

    client_type = httpx.AsyncClient

    def client(**kwargs):
        return client_type(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr("illo.brain.client.httpx.AsyncClient", client)
    request = RunRequest(
        run_id=1,
        system_prompt="Read the ledger",
        user_message="Status?",
        mcp_url="http://illo/brain/mcp",
        mcp_token="test-run-token",
        runtime="fake",
        model="fake",
        max_turns=3,
        timeout_s=5,
    )
    events = [event async for event in BrainClient("http://brain").run(request)]
    assert [e.type for e in events] == ["started", "text", "final"]
    assert events[-1].text == "Done" and events[-1].usage == {"input_tokens": 3}
    assert received == [request.wire()]
