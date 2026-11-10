"""HTTP contract for the separate brain service."""

import json
import os
import re
from collections.abc import AsyncIterator
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class ProviderAuth(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    kind: Literal["api_key", "chatgpt"] = "api_key"
    access_token: SecretStr | None = None
    account_id: SecretStr | None = None

    @model_validator(mode="after")
    def valid_credential(self):
        values = (self.access_token, self.account_id)
        if self.kind == "chatgpt" and not all(v and v.get_secret_value() for v in values):
            raise ValueError("ChatGPT needs an access token and an account ID")
        if self.kind == "api_key" and any(v is not None for v in values):
            raise ValueError("API-key mode uses only the brain's environment key")
        return self

    def wire(self):
        if self.kind == "api_key":
            return {"kind": "api_key"}
        return {
            "kind": "chatgpt",
            "access_token": self.access_token.get_secret_value(),
            "account_id": self.account_id.get_secret_value(),
        }


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    run_id: int
    system_prompt: str
    user_message: str
    mcp_url: str
    mcp_token: str = Field(repr=False)
    runtime: str
    model: str
    max_turns: int
    timeout_s: float
    provider_auth: ProviderAuth = Field(default_factory=ProviderAuth, repr=False)
    api_key_fallback: bool = False
    smoke: bool = False

    def wire(self):
        data = self.model_dump()
        data["provider_auth"] = self.provider_auth.wire()
        return data


class BrainEvent(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["started", "text", "tool_call", "final", "error"]
    text: str = ""
    usage: dict[str, Any] = Field(default_factory=dict)
    category: Literal["auth", "timeout", "runtime", "limit"] = "runtime"
    code: str = "unknown"
    resets_at: int | None = None
    api_key_fallback_failed: bool = False

    def failure_reason(self) -> str:
        code = self.code if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", self.code) else "unknown"
        return f"The brain run failed: {self.category} {code}"


class BrainClient:
    """Stream JSON lines. Never log the request because it contains a run token."""

    def __init__(self, url: str, secret: str | None = None, *, transport=None):
        self.transport = transport
        self.url = url.rstrip("/")
        self._secret = secret if secret is not None else os.getenv("ILLO_BRAIN_SECRET", "")

    async def run(self, request: RunRequest) -> AsyncIterator[BrainEvent]:
        """The outer run manager also enforces the full wall-clock timeout."""
        options = {"transport": self.transport} if self.transport is not None else {}
        async with httpx.AsyncClient(timeout=request.timeout_s, **options) as client:
            async with client.stream(
                "POST",
                f"{self.url}/run",
                json=request.wire(),
                headers={"X-Illo-Brain-Secret": self._secret},
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.strip():
                        yield BrainEvent.model_validate(json.loads(line))
