"""A scripted brain that uses the real HTTP tool endpoint."""

import json
from collections.abc import AsyncIterator

import httpx

from illo.brain.client import BrainEvent, RunRequest


class FakeBrain:
    """Use ASGITransport in tests, or a network HTTP URL for local checks."""

    def __init__(
        self, script: list[dict] | None = None, *, transport=None, final_text: str | None = None
    ):
        self.script = script if script is not None else [{"name": "lanes_search", "arguments": {}}]
        self.transport = transport
        self.final_text = final_text
        self.requests: list[RunRequest] = []
        self.results: list[dict] = []

    async def run(self, request: RunRequest) -> AsyncIterator[BrainEvent]:
        """A failed HTTP call fails the run instead of pretending to use a tool."""
        self.requests.append(request)
        results = []
        yield BrainEvent(type="started")
        async with httpx.AsyncClient(transport=self.transport, timeout=request.timeout_s) as client:
            for index, call in enumerate(self.script, 1):
                yield BrainEvent(type="tool_call", name=call["name"])
                response = await client.post(
                    request.mcp_url,
                    headers={"Authorization": f"Bearer {request.mcp_token}"},
                    json={"jsonrpc": "2.0", "id": index, "method": "tools/call", "params": call},
                )
                response.raise_for_status()
                payload = response.json()
                if "error" in payload:
                    raise RuntimeError("The fake brain tool request failed")
                self.results.append(payload["result"])
                results.append(payload["result"])
        yield BrainEvent(
            type="final",
            text=self.final_text
            if self.final_text is not None
            else json.dumps(results, default=str),
        )
