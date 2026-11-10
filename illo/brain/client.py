"""HTTP contract for the separate brain service."""

import json
import os
import re
from collections.abc import AsyncIterator
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: int
    system_prompt: str
    user_message: str
    mcp_url: str
    mcp_token: str = Field(repr=False)
    runtime: str
    model: str
    max_turns: int
    timeout_s: float


class BrainEvent(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["started", "text", "tool_call", "final", "error"]
    text: str = ""
    usage: dict[str, Any] = Field(default_factory=dict)
    category: Literal["auth", "timeout", "runtime", "limit"] = "runtime"
    code: str = "unknown"

    def failure_reason(self) -> str:
        code = self.code if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", self.code) else "unknown"
        return f"The brain run failed: {self.category} {code}"


class BrainClient:
    """Stream JSON lines. Never log the request because it contains a run token."""

    def __init__(self, url: str, secret: str | None = None):
        self.url = url.rstrip("/")
        self._secret = secret if secret is not None else os.getenv("ILLO_BRAIN_SECRET", "")

    async def run(self, request: RunRequest) -> AsyncIterator[BrainEvent]:
        """The outer run manager also enforces the full wall-clock timeout."""
        async with httpx.AsyncClient(timeout=request.timeout_s) as client:
            async with client.stream(
                "POST",
                f"{self.url}/run",
                json=request.model_dump(),
                headers={"X-Illo-Brain-Secret": self._secret},
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.strip():
                        yield BrainEvent.model_validate(json.loads(line))
