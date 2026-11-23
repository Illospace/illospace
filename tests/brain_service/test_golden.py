"""Golden runs: each wire gives the same events and the same runs.usage value.

A golden case starts a real run in the app run manager. The run manager calls the brain
service. The brain runs its loop with a fake provider transport and fake Illo tools. The
case records the brain events, the provider requests, the tool calls and the runs row.

Set ILLO_GOLDEN_RECORD=1 to write the fixture files again. Review each changed line.
"""

import base64
import json
import os
from pathlib import Path

import httpx
import pytest
from cryptography.fernet import Fernet
from mcp.shared.exceptions import McpError
from mcp.types import CallToolResult, ErrorData, Tool
from pydantic import SecretStr

from brain.models import RunRequest as ServiceRequest
from brain.runtimes import openai_compat, openai_responses
from brain.server import create_app as create_brain
from brain.worker import failure_event
from illo.brain.client import BrainClient
from illo.connectors import chatgpt
from illo.db import one
from tests.chatgpt.conftest import FakeTokenEndpoint, save_grant

FIXTURES = Path(__file__).parent / "golden"
RECORD = os.getenv("ILLO_GOLDEN_RECORD") == "1"
WIRES = {"responses": "openai_responses", "chat": "openai_compat"}
SYSTEM = "Use the Acme tools."
IMAGE = base64.b64encode(b"\x89PNG\r\n\x1a\nAcme golden image").decode()
TOOLS = [
    Tool(
        name="lanes_search",
        description="Search Acme lanes",
        inputSchema={"type": "object", "properties": {"query": {"type": "string"}}},
    ),
    Tool(name="asks_list", description="List Acme asks", inputSchema={"type": "object"}),
]
CLOSED = {"customer_read": False, "personal_read": False}


def result(text, *, flags=CLOSED, error=False):
    value = {"content": [{"type": "text", "text": text}], "isError": error}
    if flags is not None:
        value["structuredContent"] = {"run_flags": flags}
    return value


def calls(*items):
    return {"calls": list(items)}


def answer(text):
    return {"text": text}


def failure(status, **error):
    return {"status": status, "error": error}


CASES = {
    "plain_answer": {"turns": [answer("Release ready.")]},
    "three_round_trips": {
        "turns": [
            calls(("lanes_search", '{"query":"release"}')),
            calls(("asks_list", "{}")),
            calls(("lanes_search", '{"query":"owner"}')),
            answer("Ada owns the release."),
        ],
        "results": [result("Lane: release"), result("Ask: review"), result("Owner: Ada")],
    },
    "limit_then_last_request": {
        "max_turns": 2,
        "turns": [
            calls(*[("lanes_search", json.dumps({"query": f"lane {i}"})) for i in range(3)]),
            answer("I checked two lanes. I did not check the third."),
        ],
    },
    "tool_error": {
        "turns": [
            calls(("lanes_search", '{"query":"x"}'), ("asks_list", "{}"), ("lanes_search", "{bad")),
            answer("One lane failed."),
        ],
        "results": [result("Lane not found", flags=None, error=True), "mcp_error"],
    },
    "customer_read_keeps_search_closed": {
        "brain": {"web_search": True},
        "turns": [calls(("lanes_search", '{"query":"Acme"}')), {"search": "Acme news."}],
        "results": [result("Customer: Acme", flags={"customer_read": True, "personal_read": False})],
    },
    "missing_flags_close_search": {
        "brain": {"web_search": True},
        "turns": [calls(("lanes_search", '{"query":"Acme"}')), {"search": "Acme news."}],
        "results": [result("Lane: Acme", flags=None)],
    },
    "closed_flags_keep_search_open": {
        "brain": {"web_search": True},
        "turns": [calls(("lanes_search", '{"query":"Acme"}')), {"search": "Acme news."}],
    },
    "subscription_limit_fallback": {
        "auth": "chatgpt",
        "fallback": True,
        "turns": [
            calls(("lanes_search", '{"query":"release"}')),
            {"limit": "http"},
            answer("Release ready."),
        ],
    },
    "subscription_limit_in_stream_fallback": {
        "auth": "chatgpt",
        "fallback": True,
        "turns": [
            calls(("lanes_search", '{"query":"release"}')),
            {"limit": "stream"},
            answer("Release ready."),
        ],
    },
    "auth_rejection_refresh": {
        "auth": "chatgpt",
        "access": "fake-old-token",
        # The API key of the Chat Completions case is rejected too. That wire has no refresh.
        "turns": [{"reject": ["Bearer fake-old-token", "Bearer fake-golden-key"],
                   **answer("Release ready.")}],
    },
    "image_refusal": {
        "brain": {"images": [{"mime": "image/png", "data": IMAGE}]},
        "turns": [{"refuse_images": True, **answer("I could not see the image.")}],
    },
    "rate_limit": {
        "turns": [failure(429, code="rate_limit_exceeded", type="requests", message="PRIVATE")]
    },
    "provider_500": {
        "turns": [failure(500, code="server_error", type="server_error", message="PRIVATE")]
    },
}


def usage(wire, n):
    if wire == "responses":
        return {
            "input_tokens": 100 * n + 10,
            "input_tokens_details": {"cached_tokens": n},
            "output_tokens": 10 * n + 5,
            "output_tokens_details": {"reasoning_tokens": n},
            "total_tokens": 110 * n + 15,
        }
    return {
        "prompt_tokens": 100 * n + 10,
        "prompt_tokens_details": {"cached_tokens": n},
        "completion_tokens": 10 * n + 5,
        "total_tokens": 110 * n + 15,
    }


def sse(*chunks):
    text = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=text)


def halves(text):
    return [text[: len(text) // 2], text[len(text) // 2 :]]


LIMIT = {"type": "usage_limit_reached", "code": "usage_limit_reached", "resets_at": 1893456000,
         "message": "PRIVATE"}


def responses_turn(turn, n, search):
    if turn.get("limit") == "http":
        return httpx.Response(429, json={"error": LIMIT})
    if turn.get("limit"):
        error = LIMIT
        return sse({"type": "response.failed",
                    "response": {"error": error, "usage": usage("responses", n)}})
    output = []
    if turn.get("calls"):
        output.append({"type": "reasoning", "id": f"rs_{n}", "summary": [],
                       "encrypted_content": f"opaque-{n}"})
    for i, (name, arguments) in enumerate(turn.get("calls", [])):
        output.append({"type": "function_call", "id": f"fc_{n}_{i}", "call_id": f"call_{n}_{i}",
                       "name": name, "arguments": arguments})
    text = turn.get("text") or turn.get("search")
    if search:
        output.append({"type": "web_search_call", "id": "ws_1", "status": "completed",
                       "action": {"type": "search", "query": "Acme"}})
    if text:
        output.append({"type": "message", "id": f"msg_{n}", "role": "assistant",
                       "content": [{"type": "output_text", "text": text}]})
    chunks = [{"type": "response.output_item.added", "output_index": i, "item": item}
              for i, item in enumerate(output)]
    chunks += [{"type": "response.output_text.delta", "delta": part} for part in halves(text or "")
               if part]
    chunks += [{"type": "response.output_item.done", "output_index": i, "item": item}
               for i, item in enumerate(output)]
    chunks.append({"type": "response.completed", "response": {
        "id": f"resp_{n}", "status": "completed", "output": output, "usage": usage("responses", n)}})
    return sse(*chunks)


def chat_turn(turn, n):
    if turn.get("limit") == "http":
        return httpx.Response(429, json={"error": LIMIT})
    if turn.get("limit"):
        return sse({"error": LIMIT})

    def choice(delta, finish=None):
        return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    chunks = [choice({"role": "assistant"})]
    text = turn.get("text") or turn.get("search")
    chunks += [choice({"content": part}) for part in halves(text or "") if part]
    for i, (name, arguments) in enumerate(turn.get("calls", [])):
        first, second = halves(arguments)
        chunks.append(choice({"tool_calls": [{"index": i, "id": f"call_{n}_{i}", "type": "function",
                                              "function": {"name": name, "arguments": first}}]}))
        chunks.append(choice({"tool_calls": [{"index": i, "function": {"arguments": second}}]}))
    chunks.append(choice({}, "tool_calls" if turn.get("calls") else "stop"))
    chunks.append({"choices": [], "usage": usage("chat", n)})
    return httpx.Response(200, text="".join("data: " + json.dumps(c) + "\n\n" for c in chunks)
                          + "data: [DONE]\n\n")


class Provider:
    def __init__(self, wire, turns):
        self.wire = wire
        self.turns = turns
        self.index = 0
        self.requests = []

    def respond(self, request):
        body = json.loads(request.content)
        self.requests.append({
            "url": str(request.url),
            "authorization": request.headers.get("authorization"),
            "account": request.headers.get("chatgpt-account-id"),
            "body": body,
        })
        turn = self.turns[self.index]
        if request.headers.get("authorization") in turn.get("reject", []):
            return httpx.Response(401, json={"error": {
                "code": "invalid_token", "type": "invalid_request_error", "message": "PRIVATE"}})
        if turn.get("refuse_images") and "input_image" in json.dumps(body):
            return httpx.Response(400, json={"error": {
                "code": "invalid_image", "type": "invalid_request_error", "message": "PRIVATE"}})
        self.index += 1
        if "status" in turn:
            return httpx.Response(turn["status"], json={"error": turn["error"]})
        if self.wire == "responses":
            search = "search" in turn and {"type": "web_search"} in body.get("tools", [])
            return responses_turn(turn, self.index, search)
        return chat_turn(turn, self.index)


class Session:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append({"name": name, "arguments": arguments})
        value = self.results.pop(0) if self.results else result("Acme release is ready")
        if value == "mcp_error":
            raise McpError(ErrorData(code=-32000, message="PRIVATE tool failure"))
        return CallToolResult.model_validate(value)


async def drive(request, session, transport):
    """The one entry point of the brain loop that the golden cases use.

    It makes the HTTP client as the run function of each wire does, without the MCP connection.
    """
    if request.runtime == "openai_compat":
        headers = {"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]}
        loop = openai_compat.conversation
    else:
        headers = {}
        loop = openai_responses.conversation
    async with httpx.AsyncClient(
        headers=headers, timeout=request.timeout_s, follow_redirects=False, transport=transport
    ) as http:
        async for item in loop(request, session, TOOLS, http):
            yield item


async def golden_run(app, replay, monkeypatch, wire, case):
    monkeypatch.setenv("OPENAI_API_KEY", "fake-golden-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://provider.example/v1")
    monkeypatch.setenv("ILLO_BRAIN_SECRET", "fake-golden-secret")
    for name in ("ILLO_BRAIN_EFFORT", "ILLO_BRAIN_OPENAI_COMPAT_REASONING_EFFORT"):
        monkeypatch.delenv(name, raising=False)
    ctx = app.state.ctx
    ctx.workspace = ctx.workspace.model_copy(deep=True)
    brain = ctx.workspace.config.brain
    brain.runtime = WIRES[wire]
    brain.model = "golden-model"
    brain.max_turns = case.get("max_turns", 4)
    brain.api_key_fallback = case.get("fallback", False)
    backend = FakeTokenEndpoint()
    if case.get("auth") == "chatgpt" and wire == "responses":
        brain.auth = "chatgpt"
        ctx.config.vault_key = SecretStr(Fernet.generate_key().decode())
        ctx.chatgpt_client = chatgpt.ChatGPTClient(httpx.MockTransport(backend.respond))
        await save_grant(ctx, access=case.get("access", "fake-access"))
    provider = Provider(wire, case["turns"])
    session = Session(case.get("results", []))
    brain_runs = []

    async def runner(body):
        request = ServiceRequest.model_validate(
            {**body.wire(), "system_prompt": SYSTEM, **case.get("brain", {})}
        )
        events = []
        brain_runs.append(events)
        try:
            async for item in drive(request, session, httpx.MockTransport(provider.respond)):
                events.append(dict(item))
                yield item
        except Exception as error:
            item = failure_event(error)
            events.append(dict(item))
            yield item

    service = create_brain(runner=runner)
    ctx.brain_client = BrainClient(
        "http://brain", "fake-golden-secret", transport=httpx.ASGITransport(app=service)
    )
    await replay(text="Find the release")
    async with ctx.pool.connection() as conn:
        run = await one(conn, "SELECT status, error, usage FROM runs ORDER BY id DESC LIMIT 1")
    return json.loads(json.dumps({
        "brain_runs": brain_runs,
        "provider_requests": provider.requests,
        "mcp_calls": session.calls,
        "refreshes": backend.refresh_count,
        "run": {"status": run["status"], "error": run["error"], "usage": run["usage"]},
    }))


@pytest.mark.parametrize("case", sorted(CASES))
@pytest.mark.parametrize("wire", sorted(WIRES))
async def test_golden_run(app, replay, monkeypatch, wire, case):
    actual = await golden_run(app, replay, monkeypatch, wire, CASES[case])
    path = FIXTURES / f"{wire}.json"
    if RECORD:
        FIXTURES.mkdir(exist_ok=True)
        recorded = json.loads(path.read_text()) if path.exists() else {}
        recorded[case] = actual
        path.write_text(json.dumps(dict(sorted(recorded.items())), indent=1) + "\n")
    expected = json.loads(path.read_text())[case]
    assert actual == expected
