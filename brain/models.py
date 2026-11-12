"""The gateway and the service share this wire format."""

import os
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr, model_validator

type BrainEvent = dict[str, Any]


class ApiKeyAuth(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    kind: Literal["api_key"] = "api_key"


class ChatGPTAuth(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    kind: Literal["chatgpt"] = "chatgpt"
    access_token: SecretStr
    account_id: SecretStr


class RunImage(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    mime: Literal["image/png", "image/jpeg", "image/gif", "image/webp"]
    data: SecretStr = Field(repr=False, exclude=True)

    def wire(self):
        return {"mime": self.mime, "data": self.data.get_secret_value()}


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, validate_assignment=True)

    run_id: str | int
    system_prompt: str = Field(max_length=100_000)
    images: list[RunImage] = Field(default_factory=list, repr=False, exclude=True)
    user_message: str = Field(max_length=100_000)
    mcp_url: HttpUrl
    mcp_token: SecretStr
    runtime: Literal["claude", "openai_responses", "openai_compat", "echo"] = "claude"
    model: str = Field(
        default_factory=lambda: os.getenv("ILLO_BRAIN_MODEL", "claude-opus-5-5"),
        min_length=1,
        max_length=128,
    )
    max_turns: int = Field(default=8, ge=1, le=50)
    timeout_s: float = Field(default=300, gt=0, le=3600)
    provider_auth: ApiKeyAuth | ChatGPTAuth = Field(
        default_factory=ApiKeyAuth, discriminator="kind", repr=False
    )
    effort: Literal["low", "medium", "high"] = "low"
    api_key_fallback: bool = False
    smoke: bool = False

    @model_validator(mode="after")
    def auth_runtime(self):
        if self.smoke and self.runtime != "openai_responses":
            raise ValueError("The smoke check needs openai_responses")
        if self.provider_auth.kind == "chatgpt" and self.runtime != "openai_responses":
            raise ValueError("ChatGPT sign-in needs the openai_responses runtime")
        if self.images and self.runtime != "openai_responses":
            object.__setattr__(self, "user_message", self.user_message +
                               "\n\nThe images could not be shown. This runtime accepts text only.")
            object.__setattr__(self, "images", [])
        return self

    def wire(self) -> dict:
        data = self.model_dump(mode="json")
        data["mcp_token"] = self.mcp_token.get_secret_value()
        if isinstance(self.provider_auth, ChatGPTAuth):
            data["provider_auth"] = {
                "kind": "chatgpt",
                "access_token": self.provider_auth.access_token.get_secret_value(),
                "account_id": self.provider_auth.account_id.get_secret_value(),
            }
        data["images"] = [image.wire() for image in self.images]
        return data


def event(kind: str, **fields: Any) -> BrainEvent:
    return {"type": kind, **fields}


class RunError(Exception):
    def __init__(self, category: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.category = category
        self.details = details
