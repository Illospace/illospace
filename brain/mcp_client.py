"""Keep the MCP token in memory and retain the session for each run."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Tool

from brain.models import RunRequest


@asynccontextmanager
async def connect(request: RunRequest) -> AsyncIterator[ClientSession]:
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
