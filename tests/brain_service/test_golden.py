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

from brain.errors import failure_event
from brain.loop import conversation
from brain.models import RunRequest as ServiceRequest
from brain.server import create_app as create_brain
from brain.worker import RUNNERS
from illo.brain.client import BrainClient
from illo.connectors import chatgpt
from illo.db import one
from illo.workspace import RouteSettings
from tests.chatgpt.conftest import FakeTokenEndpoint, save_grant

FIXTURES = Path(__file__).parent / "golden"
RECORD = os.getenv("ILLO_GOLDEN_RECORD") == "1"
# The provider of each wire. `local` is a custom Chat Completions provider.
WIRES = {"responses": "openai", "chat": "local"}
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
        "max_tool_calls": 2,
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
    # The model text holds the API key. Responses stops the run. Chat Completions completes
    # it, and the service redacts the key in the stream.
    "credential_echo": {"delivered": True, "turns": [answer("fake-golden-key. Ask Ada for the key.")]},
    # The output limit cuts the response. Only the Responses error keeps the usage.
    "output_limit": {"turns": [{"length": True, **answer("Release rea")}]},
    # The second call of a batch has no ID. The first call runs before the run fails.
    "missing_call_id": {
        "turns": [{**calls(("lanes_search", '{"query":"release"}'), ("asks_list", "{}")),
                   "no_id": [1]}],
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
    if turn.get("length"):
        return sse({"type": "response.output_text.delta", "delta": turn["text"]},
                   {"type": "response.incomplete", "response": {
                       "id": f"resp_{n}", "status": "incomplete", "usage": usage("responses", n)}})
    output = []
    if turn.get("calls"):
        output.append({"type": "reasoning", "id": f"rs_{n}", "summary": [],
                       "encrypted_content": f"opaque-{n}"})
    for i, (name, arguments) in enumerate(turn.get("calls", [])):
        call_id = None if i in turn.get("no_id", []) else f"call_{n}_{i}"
        output.append({"type": "function_call", "id": f"fc_{n}_{i}", "call_id": call_id,
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
        start = {"index": i, "id": f"call_{n}_{i}", "type": "function",
                 "function": {"name": name, "arguments": first}}
        if i in turn.get("no_id", []):
            del start["id"]
        chunks.append(choice({"tool_calls": [start]}))
        chunks.append(choice({"tool_calls": [{"index": i, "function": {"arguments": second}}]}))
    finish = "length" if turn.get("length") else "tool_calls" if turn.get("calls") else "stop"
    chunks.append(choice({}, finish))
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


async def drive(request, provider, fallback, session, transport):
    """The one entry point of the brain loop that the golden cases use.

    It runs the loop with the worker's adapter for the wire, without the MCP connection.
    """
    async with httpx.AsyncClient(
        timeout=request.timeout_s, follow_redirects=False, transport=transport
    ) as http:
        wire = RUNNERS[provider.wire].args[0]
        async for item in conversation(wire, request, provider, session, TOOLS, http, fallback):
            yield item


async def golden_run(app, replay, monkeypatch, wire, case):
    monkeypatch.setenv("ILLO_PROVIDER_OPENAI_API_KEY", "fake-golden-key")
    monkeypatch.setenv("ILLO_PROVIDER_OPENAI_BASE_URL", "https://provider.example/v1")
    monkeypatch.setenv("ILLO_PROVIDER_LOCAL_API_KEY", "fake-golden-key")
    monkeypatch.setenv("ILLO_PROVIDER_LOCAL_WIRE", "chat")
    monkeypatch.setenv("ILLO_PROVIDER_LOCAL_BASE_URL", "https://provider.example/v1")
    monkeypatch.setenv("ILLO_BRAIN_SECRET", "fake-golden-secret")
    ctx = app.state.ctx
    ctx.workspace = ctx.workspace.model_copy(deep=True)
    brain = ctx.workspace.config.brain
    # The chat provider takes no effort. The Responses runs keep the effort of a DM.
    default = RouteSettings(provider=WIRES[wire], model="golden-model",
                            effort="medium" if wire == "responses" else None,
                            max_tool_calls=case.get("max_tool_calls", 4), timeout_s=120)
    brain.routes = {"default": default}
    brain.chatgpt.fallback = "openai" if case.get("fallback") else "none"
    backend = FakeTokenEndpoint()
    if case.get("auth") == "chatgpt" and wire == "responses":
        default.provider = "chatgpt"
        ctx.config.vault_key = SecretStr(Fernet.generate_key().decode())
        ctx.chatgpt_client = chatgpt.ChatGPTClient(httpx.MockTransport(backend.respond))
        await save_grant(ctx, access=case.get("access", "fake-access"))
    provider = Provider(wire, case["turns"])
    session = Session(case.get("results", []))
    brain_runs = []

    async def runner(body, selected, fallback):
        request = ServiceRequest.model_validate(
            {**body.wire(), "system_prompt": SYSTEM, **case.get("brain", {})}
        )
        events = []
        brain_runs.append(events)
        try:
            async for item in drive(request, selected, fallback, session,
                                    httpx.MockTransport(provider.respond)):
                events.append(dict(item))
                yield item
        except Exception as error:
            item = failure_event(error)
            events.append(dict(item))
            yield item

    service = create_brain(runner=runner)
    client = BrainClient(
        "http://brain", "fake-golden-secret", transport=httpx.ASGITransport(app=service)
    )
    ctx.providers = await client.providers()
    delivered = []

    class Recorder:
        """Record the events after the service redaction, as the app gets them."""

        async def run(self, request):
            async for item in client.run(request):
                delivered.append(item.model_dump(mode="json"))
                yield item

    ctx.brain_client = Recorder()
    await replay(text="Find the release")
    async with ctx.pool.connection() as conn:
        run = await one(conn, "SELECT status, error, usage FROM runs ORDER BY id DESC LIMIT 1")
    extra = {"delivered": delivered} if case.get("delivered") else {}
    return json.loads(json.dumps({
        **extra,
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
