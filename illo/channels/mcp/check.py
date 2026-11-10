"""Check an agent connection without a model or a write tool."""

from pathlib import Path

import httpx


async def check(url: str, token_file: Path, *, transport=None) -> int:
    token = token_file.expanduser().read_text().strip()
    if not token:
        raise ValueError("The token file is empty")
    url = url.rstrip("/")
    if not url.endswith("/mcp"):
        url += "/mcp"
    calls = (
        (
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "illo-check", "version": "0.1.0"},
            },
        ),
        ("tools/list", {}),
        ("tools/call", {"name": "lane_search", "arguments": {"limit": 1}}),
    )
    async with httpx.AsyncClient(timeout=15, transport=transport, trust_env=False) as client:
        for request_id, (method, params) in enumerate(calls, 1):
            response = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json, text/event-stream",
                },
                json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
            )
            if response.status_code == 401:
                raise ValueError("The server rejected the token. Check the token file.")
            response.raise_for_status()
            body = response.json()
            if body.get("error") or body.get("result", {}).get("isError"):
                raise ValueError(f"The MCP check failed at {method}.")
            result = body.get("result", {})
            if method == "initialize" and not result.get("protocolVersion"):
                raise ValueError("The server did not return an MCP version.")
            if method == "tools/list" and not any(
                tool.get("name") == "lane_search" for tool in result.get("tools", [])
            ):
                raise ValueError("The server did not list lane_search.")
    print("MCP initialize: ok.\nMCP tool list: ok.\nMCP lane read: ok.")
    return 0
