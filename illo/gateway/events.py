"""Channel adapters supply data, not authority."""

from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr


class InboundEvent(BaseModel):
    """The external ID must be stable across channel retries."""

    model_config = ConfigDict(extra="forbid")
    _slack_context_messages: list[dict] = PrivateAttr(default_factory=list)
    channel: str
    external_id: str
    kind: str
    sender: str = Field(default="", repr=False)
    text: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    reply_to: str | None = None


class ChannelAdapter(Protocol):
    """The gateway owns the first sign of work and the final delivery."""

    async def show_working(self, event: InboundEvent) -> None: ...
    async def deliver_final(self, event: InboundEvent, text: str) -> None: ...
    async def deliver_error(self, event: InboundEvent, *, category="internal") -> None: ...
