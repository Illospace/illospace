"""HTTP contract for the brain service. `brain/models.py` is the service copy of the request."""

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


class RunImage(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    mime: Literal["image/png", "image/jpeg", "image/gif", "image/webp"]
    data: SecretStr = Field(repr=False, exclude=True)

    def wire(self):
        return {"mime": self.mime, "data": self.data.get_secret_value()}


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    run_id: int
    system_prompt: str
    images: list[RunImage] = Field(default_factory=list, repr=False, exclude=True)
    user_message: str
    mcp_url: str
    mcp_token: str = Field(repr=False)
    provider: str
    model: str
    # The provider lists its effort names, including minimal when it supports it.
    effort: str | None = Field(default=None, pattern=r"^[a-z]{1,16}$")
    max_tool_calls: int
    timeout_s: float
    max_output_tokens: int | None = Field(default=None, ge=1)
    web_search: bool = False
    provider_auth: ProviderAuth = Field(default_factory=ProviderAuth, repr=False)
    fallback_provider: str | None = None
    smoke: bool = False

    def wire(self):
        data = self.model_dump(exclude={"smoke"} if not self.smoke else set())
        data["provider_auth"] = self.provider_auth.wire()
        data["images"] = [image.wire() for image in self.images]
        return data


class BrainEvent(BaseModel):
    model_config = ConfigDict(extra="allow")
    type: Literal["started", "text", "tool_call", "web_search", "payer", "response", "final", "error"]
    text: str = ""
    usage: dict[str, Any] = Field(default_factory=dict)
    category: Literal["auth", "timeout", "runtime", "limit", "config", "internal", "transient"] = "runtime"
    code: str = "unknown"
    resets_at: int | None = None
    fallback_failed: bool = False

    def failure_reason(self) -> str:
        code = self.code if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", self.code) else "unknown"
        return f"The brain run failed: {self.category} {code}"


def provider_table(description: dict) -> dict[str, dict]:
    """Index `GET /providers` (or `python -m brain.providers`) by provider name."""
    return {item["name"]: item for item in description["providers"]}


class BrainClient:
    """Stream JSON lines. Never log the request because it contains a run token."""

    def __init__(self, url: str, secret: str | None = None, *, transport=None):
        self.transport = transport
        self.url = url.rstrip("/")
        self._secret = secret if secret is not None else os.getenv("ILLO_BRAIN_SECRET", "")

    def client(self, timeout: float) -> httpx.AsyncClient:
        options = {"transport": self.transport} if self.transport is not None else {}
        return httpx.AsyncClient(base_url=self.url, timeout=timeout,
                                 headers={"X-Illo-Brain-Secret": self._secret}, **options)

    async def providers(self) -> dict[str, dict]:
        """The brain's one description of its providers: name, wire, and capabilities."""
        async with self.client(10) as client:
            response = await client.get("/providers")
            response.raise_for_status()
        return provider_table(response.json())

    async def transcribe(self, provider: str, model: str, extension: str, audio: bytes,
                         timeout: float) -> str | None:
        async with self.client(timeout) as client:
            response = await client.post("/transcribe", content=audio, params={
                "provider": provider, "model": model, "extension": extension})
        text = response.json().get("text") if response.status_code == 200 else None
        return text if isinstance(text, str) and text.strip() else None

    async def run(self, request: RunRequest) -> AsyncIterator[BrainEvent]:
        """The outer run manager also enforces the full wall-clock timeout."""
        async with self.client(request.timeout_s) as client:
            async with client.stream("POST", "/run", json=request.wire()) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.strip():
                        yield BrainEvent.model_validate(json.loads(line))
