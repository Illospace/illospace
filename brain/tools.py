"""The Illo MCP boundary. The model sees only the run's tools, and only checked calls reach MCP."""

import json
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import McpError
from mcp.types import CallToolResult, TextContent, Tool

from brain.errors import RunError
from brain.models import ChatGPTAuth, RunRequest

# The Agent SDK names each MCP tool "mcp__<server>__<tool>". It gets only this server.
SDK_SERVER = "illo"
SDK_PREFIX = f"mcp__{SDK_SERVER}__"

SMOKE_TOOL = Tool(
    name="smoke_step",
    description="Run step n. For steps 2 and 3, send the token from the previous step.",
    inputSchema={
        "type": "object",
        "properties": {
            "n": {"type": "integer", "minimum": 1, "maximum": 3},
            "token": {"type": "string"},
        },
        "required": ["n"],
        "additionalProperties": False,
    },
)


class SmokeSession:
    """Run a private challenge without a connection to the team's MCP tools."""

    def __init__(self):
        self.steps = 0
        self.token = None
        self.responses = {}

    def observe_response(self, call_ids, response_id=None):
        if response_id is None:
            response_id = len(self.responses)
        calls = self.responses.setdefault(response_id, set())
        calls.update(call_ids)
        if len(calls) > 1:
            raise RunError("runtime", "The smoke check needs one tool call in each response")

    def completed_steps(self):
        return min(self.steps, sum(bool(calls) for calls in self.responses.values()))

    async def call_tool(self, name, arguments):
        step = arguments.get("n")
        if (name != SMOKE_TOOL.name or type(step) is not int or step != self.steps + 1
                or step > 3 or arguments.get("token") != self.token
                or arguments.keys() - {"n", "token"}):
            raise RunError("runtime", "The smoke tool received the wrong step or token")
        self.steps = step
        self.token = secrets.token_urlsafe(24)
        return CallToolResult(content=[TextContent(type="text", text=self.token)])


@dataclass
class Call:
    """One tool call that the model asks for. The arguments are JSON text from the provider."""

    id: str | None
    name: str | None
    arguments: Any


@asynccontextmanager
async def connect(request: RunRequest) -> AsyncIterator[ClientSession]:
    """Keep the MCP token in memory and keep one session for the run."""
    headers = {"Authorization": "Bearer " + request.mcp_token.get_secret_value()}
    async with httpx.AsyncClient(
        headers=headers, timeout=request.timeout_s, follow_redirects=False
    ) as http:
        async with streamable_http_client(str(request.mcp_url), http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                yield session


async def list_tools(session: ClientSession) -> list[Tool]:
    tools = []
    cursor = None
    seen = set()
    while True:
        result = await session.list_tools(cursor=cursor)
        tools.extend(result.tools)
        cursor = result.nextCursor
        if cursor is None:
            return tools
        if cursor in seen:
            raise ValueError("The MCP tool list repeats its cursor")
        seen.add(cursor)


def check_call(call: Call, names: set[str], missing_id: str) -> None:
    """Refuse a call outside the run catalog. The wire gives the message for a call with no ID."""
    if call.name not in names:
        raise RunError("runtime", "The model requested a tool outside the run catalog")
    if not call.id:
        raise RunError("runtime", missing_id)


def check_sdk_tool(name: str) -> None:
    if not name.startswith(SDK_PREFIX):
        raise RunError("runtime", "The runtime exposed a tool outside Illo")


def check_echo(chunk: dict, key: str | None, request: RunRequest | None = None, *,
               error: bool = False) -> None:
    """Refuse a provider reply that echoes a credential, before an argument can reach MCP.

    Runs and transcriptions use it. An error chunk can name the rejected credential. Its error
    mapping never copies text.
    """
    encoded = json.dumps(chunk)
    images = request.images if request else ()
    if any(image.data.get_secret_value() in encoded for image in images):
        raise RunError("runtime", "The provider returned private image data")
    values = [key]
    if request and isinstance(request.provider_auth, ChatGPTAuth):
        values.append(request.provider_auth.access_token.get_secret_value())
    if not error and any(value and json.dumps(value)[1:-1] in encoded for value in values):
        raise RunError("runtime", "The provider returned a credential")


def model_text(result: dict) -> str:
    """Give only MCP content to the model. Structured metadata is for clients."""
    text = "\n".join(block["text"] for block in result.get("content", [])
                     if block.get("type") == "text")
    return ("Tool error.\n" if result.get("isError") else "") + text


async def execute(session, request: RunRequest, call: Call, *, run_flags: bool) -> str:
    """Run one checked call and give the result text. MCP failures stay tool results."""
    try:
        arguments = json.loads(call.arguments)
        if not isinstance(arguments, dict):
            raise TypeError("Tool arguments must be an object")
    except (ValueError, TypeError):
        result = {"isError": True, "content": [{"type": "text", "text": "Invalid tool arguments"}]}
    else:
        try:
            result = await session.call_tool(call.name, arguments)
            result = result.model_dump(mode="json", exclude_none=True)
        except McpError:
            result = {"isError": True, "content": [{"type": "text", "text": "The MCP tool failed"}]}
    # The app makes the run flags. A customer or personal read closes web search for the
    # rest of the run. Missing metadata also closes it, so a lost result cannot reopen search.
    if run_flags:
        flags = result.get("structuredContent", {}).get("run_flags", {})
        if flags.get("customer_read") is not False or flags.get("personal_read") is not False:
            request.web_search = False
    return model_text(result)
