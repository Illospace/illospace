"""Channel adapters supply data, not authority."""

from typing import Any, Protocol

from psycopg.types.json import Jsonb
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from illo.db import Connection, one
from illo.gateway.principal import Authority


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


async def record_event(
    conn: Connection, event: InboundEvent, payload: dict, *, authority: Authority | None = None
) -> int | None:
    """Store an event once and return its ID."""
    row = await one(
        conn,
        """INSERT INTO events (channel,external_id,kind,principal,person_id,payload)
        VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (channel,external_id) DO NOTHING RETURNING id""",
        (event.channel, event.external_id, event.kind, authority.principal if authority else None,
         authority.person_id if authority else None, Jsonb(payload)),
    )
    return row["id"] if row else None


class ChannelAdapter(Protocol):
    """The gateway owns the first sign of work and the final delivery."""

    async def show_working(self, event: InboundEvent) -> None: ...
    async def deliver_final(self, event: InboundEvent, text: str) -> None: ...
    async def deliver_error(self, event: InboundEvent, *, category="internal") -> None: ...
