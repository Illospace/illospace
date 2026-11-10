"""The tests use a local MCP server and no model credentials."""

import asyncio
import socket
import time
from contextlib import asynccontextmanager

import pytest
import pytest_asyncio
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from brain.models import RunRequest

TOOLS = [
    {
        "name": "lanes_search",
        "description": "Search Acme lanes",
        "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}},
    },
    {
        "name": "asks_list",
        "description": "List Acme asks",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "slack_post_status",
        "description": "Post a status",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
    },
]


@pytest.fixture
def run_request():
    return RunRequest(
        run_id="acme-run",
        system_prompt="Use the Acme tools.",
        user_message="Find the release",
        mcp_url="http://127.0.0.1:1/mcp",
        mcp_token="fake-run-token",
        runtime="echo",
        model="test-model",
        timeout_s=5,
    )


@asynccontextmanager
async def serve(app):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="critical", access_log=False, lifespan="off")
    )
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        while not server.started:
            if task.done():
                await task
            await asyncio.sleep(0.01)
        yield f"http://127.0.0.1:{sock.getsockname()[1]}"
    finally:
        server.should_exit = True
        await task
        sock.close()


class FakeMCP:
    def __init__(self):
        self.app = FastAPI()
        self.calls = []
        self.call_times = []
        self.tool_error = False
        self.tokens = []
        self.page = False
        self.app.add_api_route("/mcp", self.handle, methods=["POST", "GET", "DELETE"])

    async def handle(self, request: Request):
        self.tokens.append(request.headers.get("authorization"))
        if request.headers.get("authorization") != "Bearer fake-run-token":
            return JSONResponse({}, status_code=401)
        if request.method != "POST":
            return JSONResponse({}, status_code=405)
        body = await request.json()
        if "id" not in body:
            return JSONResponse({}, status_code=202)
        method = body["method"]
        result = {}
        if method == "initialize":
            result = {
                "protocolVersion": body["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "acme", "version": "1"},
            }
        elif method == "tools/list":
            if self.page:
                result = (
                    {"tools": TOOLS[:1], "nextCursor": "page2"}
                    if not body.get("params", {}).get("cursor")
                    else {"tools": TOOLS[1:]}
                )
            else:
                result = {"tools": TOOLS}
        elif method == "tools/call":
            self.calls.append(body["params"])
            self.call_times.append(time.perf_counter())
            result = {
                "content": [{"type": "text", "text": "Acme release is ready"}],
                "isError": self.tool_error,
            }
        return JSONResponse({"jsonrpc": "2.0", "id": body["id"], "result": result})


@pytest_asyncio.fixture
async def fake_mcp():
    fake = FakeMCP()
    async with serve(fake.app) as base:
        fake.url = base + "/mcp"
        yield fake
