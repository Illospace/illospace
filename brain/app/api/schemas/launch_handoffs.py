from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class LaunchHandoffCreateRequest(BaseModel):
    title: str = Field(min_length=1)
    instructions: str = Field(min_length=1)
    target_tool: str = "codex"
    summary: str | None = None
    source_surface: str = "webapp"
    source_ref: dict[str, Any] = Field(default_factory=dict)
    context_parts: list[dict[str, Any]] = Field(default_factory=list)
    acceptance_criteria: list[Any] = Field(default_factory=list)
    repo_origin_url: str | None = None
    branch_hint: str | None = None
    idempotency_key: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
