"""The wire contract of the service. `illo/brain/client.py` is the app copy.

`tests/brain_service/test_client_contract.py` compares the two copies. It names each
difference that exists today, and fails on any other difference.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr

type BrainEvent = dict[str, Any]
type ErrorCategory = Literal["auth", "timeout", "runtime", "limit", "config", "internal", "transient"]


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
    # The app resolves the route. `brain/providers.py` checks it against the provider table.
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, validate_assignment=True)

    run_id: str | int
    system_prompt: str = Field(max_length=100_000)
    images: list[RunImage] = Field(default_factory=list, repr=False, exclude=True)
    user_message: str = Field(max_length=100_000)
    mcp_url: HttpUrl
    mcp_token: SecretStr
    provider: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    model: str = Field(min_length=1, max_length=128)
    # The provider lists its effort names, including minimal when it supports it.
    effort: str | None = Field(default=None, pattern=r"^[a-z]{1,16}$")
    max_tool_calls: int = Field(default=8, ge=1, le=200)
    timeout_s: float = Field(default=300, gt=0, le=3600)
    max_output_tokens: int | None = Field(default=None, ge=1)
    web_search: bool = False
    provider_auth: ApiKeyAuth | ChatGPTAuth = Field(default_factory=ApiKeyAuth, discriminator="kind",
                                                    repr=False)
    fallback_provider: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,31}$")
    smoke: bool = False

    def wire(self) -> dict:
        data = self.model_dump(mode="json")
        data["mcp_token"] = self.mcp_token.get_secret_value()
        if isinstance(auth := self.provider_auth, ChatGPTAuth):
            data["provider_auth"] = {"kind": "chatgpt", "access_token": auth.access_token.get_secret_value(),
                                     "account_id": auth.account_id.get_secret_value()}
        data["images"] = [image.wire() for image in self.images]
        return data


def event(kind: str, **fields: Any) -> BrainEvent:
    return {"type": kind, **fields}
