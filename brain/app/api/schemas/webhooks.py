from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


class WebhookEnvelopeCreate(BaseModel):
    origin: str = Field(min_length=1, max_length=240)
    payload: dict[str, Any] = Field(default_factory=dict)
    kind: str = Field(default="signal", max_length=40)
    summary: str | None = Field(default=None, max_length=2000)
    hints: dict[str, Any] = Field(default_factory=dict)
    desired_outcome: str | None = Field(default=None, max_length=120)
    idempotency_key: str | None = Field(default=None, max_length=160)

    @field_validator("origin", "kind", mode="before")
    @classmethod
    def _strip_required_text(cls, value: Any) -> str:
        return str(value or "").strip()

    @field_validator("summary", "desired_outcome", "idempotency_key", mode="before")
    @classmethod
    def _strip_optional_text(cls, value: Any) -> str | None:
        text = str(value or "").strip()
        return text or None
