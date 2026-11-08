"""Local Streamable HTTP peer used by external MCP tests."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
import socket
import threading
import time
from types import SimpleNamespace
from urllib.parse import parse_qs
from unittest.mock import AsyncMock

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
import pytest
import uvicorn

from brain.systems.external_mcp import runtime, tokens
from brain.systems.external_mcp.client import PROTOCOL_VERSION

CLIENT_SECRET = "fixture-client-secret"
CLIENT_ID = "fixture-client-id"


@pytest.fixture
def mcp_server(monkeypatch):
    monkeypatch.setenv("ILLO_EXTERNAL_MCP_ALLOW_PRIVATE_HTTP", "1")
    app = FastAPI()
    state = SimpleNamespace(token_requests=0, token_forms=[], calls=[], requests=[], token="", unauthorized_calls=0,
                            require_auth=True, sse=False, token_lifetime=600, token_error=None, tool_error=False, rpc_error=False,
                            delay=0, tools=[
                                {"name": "get_report", "description": "Read a report", "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}}},
                                {"name": "update_report", "description": "Update a report", "inputSchema": {"type": "object"}},
                            ], result={"content": [{"type": "text", "text": "Report data"}], "structuredContent": {"total": 3}})

    @app.post("/token")
    async def token(request: Request):
        state.token_requests += 1
        state.token_forms.append(parse_qs((await request.body()).decode()))
        assert request.headers["authorization"] == "Basic " + base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        if state.token_error:
            return JSONResponse(state.token_error, status_code=401)
        state.token = f"fixture-token-{state.token_requests}"
        return JSONResponse({"access_token": state.token, "token_type": "Bearer", "expires_in": state.token_lifetime},
                            headers={"X-Echo-Credential": state.token})

    @app.post("/mcp")
    async def mcp(request: Request):
        payload = await request.json()
        method = payload["method"]
        state.requests.append(method)
        if state.require_auth and request.headers.get("authorization") != f"Bearer {state.token}":
            return Response(status_code=401)
        if method != "initialize":
            assert request.headers["mcp-protocol-version"] == PROTOCOL_VERSION
            assert request.headers["mcp-session-id"] == "fixture-session"
        if method == "notifications/initialized":
            return Response(status_code=202)
        if method == "initialize":
            result = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}}, "serverInfo": {"name": "reports", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": state.tools}
        elif method == "tools/call":
            state.calls.append(payload["params"])
            if state.unauthorized_calls:
                state.unauthorized_calls -= 1
                return Response(status_code=401)
            if state.delay:
                import asyncio
                await asyncio.sleep(state.delay)
            result = {**state.result, "isError": state.tool_error}
        else:
            return JSONResponse({"jsonrpc": "2.0", "id": payload["id"], "error": {"code": -32601, "message": "Unknown method"}})
        response = {"jsonrpc": "2.0", "id": payload["id"], "result": result}
        if state.rpc_error and method == "tools/call":
            response = {"jsonrpc": "2.0", "id": payload["id"], "error": {"code": -32000, "message": "Report is unavailable"}}
        if state.sse and method == "tools/call":
            async def stream():
                yield ': keepalive\n\ndata: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
                raw = json.dumps(response)
                yield f"event: message\ndata: {raw[:12]}\n"
                yield f"data: {raw[12:]}\n\n"
            return StreamingResponse(stream(), media_type="text/event-stream")
        return JSONResponse(response, headers={"Mcp-Session-Id": "fixture-session"})

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and time.monotonic() < deadline:
        time.sleep(.01)
    assert server.started
    state.url = f"http://127.0.0.1:{port}/mcp"
    state.token_url = f"http://127.0.0.1:{port}/token"
    try:
        yield state
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        assert not thread.is_alive()


@pytest.fixture
def configured_mcp(monkeypatch, mcp_server):
    server = SimpleNamespace(id=17, org_id="org-test", name="reports", url=mcp_server.url, token_url=mcp_server.token_url,
                             auth_mode="client_credentials", bearer_token_key=None, client_id_key="client_id",
                             client_secret_key="client_secret", scope="reports.read", resource="https://mcp.example.com/",
                             timeout_seconds=2, allowed_tools=["get_report"], enabled=True,
                             created_by="user-test", updated_by="user-test", updated_at=datetime.now(timezone.utc))
    values = {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "bearer": "fixture-bearer"}
    async def secret(key, **kwargs):
        from brain.systems.vault.runtime_secrets import RuntimeSecretUnavailable
        assert kwargs["access"] == "service"
        assert kwargs["allow_env_fallback"] is False
        assert kwargs["context"].org_id == server.org_id
        if key not in values:
            raise RuntimeSecretUnavailable("not available")
        return values[key]
    monkeypatch.setattr(tokens, "read_runtime_secret", secret)
    monkeypatch.setattr(tokens, "TOKEN_CACHE", tokens.TokenCache())
    runtime._TOOL_CACHE.clear()
    runtime._FAILURE_CACHE.clear()
    monkeypatch.setattr(runtime, "load_servers", AsyncMock(return_value=[server]))
    monkeypatch.setattr(runtime, "load_server", AsyncMock(side_effect=lambda *a: server))
    audit = AsyncMock()
    monkeypatch.setattr(runtime, "record_tool_call", audit)
    return SimpleNamespace(server=server, peer=mcp_server, audit=audit, values=values)
