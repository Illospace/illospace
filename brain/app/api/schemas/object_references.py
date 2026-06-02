from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class LinkPreviewResolveRequest(BaseModel):
    urls: list[str] = Field(default_factory=list, max_length=50)


class LinkPreviewResolveResponse(BaseModel):
    previews: list[dict[str, Any]] = Field(default_factory=list)
