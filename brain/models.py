"""The gateway and the service share this wire format."""

import os
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr

type BrainEvent = dict[str, Any]


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True, validate_assignment=True)

    run_id: str | int
    system_prompt: str = Field(max_length=100_000)
    user_message: str = Field(max_length=100_000)
    mcp_url: HttpUrl
    mcp_token: SecretStr
    runtime: Literal["claude", "openai_compat", "echo"] = "claude"
    model: str = Field(
        default_factory=lambda: os.getenv("ILLO_BRAIN_MODEL", "claude-opus-5-5"),
        min_length=1,
        max_length=128,
    )
    max_turns: int = Field(default=8, ge=1, le=50)
    timeout_s: float = Field(default=120, gt=0, le=3600)

    def wire(self) -> dict:
        data = self.model_dump(mode="json")
        data["mcp_token"] = self.mcp_token.get_secret_value()
        return data


def event(kind: str, **fields: Any) -> BrainEvent:
    return {"type": kind, **fields}


class RunError(Exception):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category
