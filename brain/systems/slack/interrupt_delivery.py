"""Deliver typed Slack interrupt replies."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping
import uuid

from brain.systems.slack.client import SlackWebClient


class SlackAcknowledgementMode(StrEnum):
    """Visible reflex acknowledgement requested by an inbound disposition."""

    SUPPRESS = "suppress"
    EYES_REACTION = "eyes_reaction"


@dataclass(frozen=True, slots=True)
class SlackInterruptReply:
    channel_id: str
    thread_ts: str | None
    text: str
    idempotency_key: str

    def to_payload(self) -> dict[str, str | None]:
        return {
            "channel_id": self.channel_id,
            "thread_ts": self.thread_ts,
            "text": self.text,
            "idempotency_key": self.idempotency_key,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "SlackInterruptReply":
        return cls(
            channel_id=str(payload.get("channel_id") or "").strip(),
            thread_ts=str(payload.get("thread_ts") or "").strip() or None,
            text=str(payload.get("text") or ""),
            idempotency_key=str(payload.get("idempotency_key") or "").strip(),
        )


@dataclass(frozen=True, slots=True)
class SlackInterruptDeliveryDirective:
    acknowledgement: SlackAcknowledgementMode
    reply: SlackInterruptReply

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": "slack_interrupt_reply",
            "acknowledgement": self.acknowledgement.value,
            "reply": self.reply.to_payload(),
        }

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, Any],
    ) -> "SlackInterruptDeliveryDirective | None":
        if payload.get("kind") != "slack_interrupt_reply":
            return None
        acknowledgement = SlackAcknowledgementMode(
            str(payload.get("acknowledgement") or "")
        )
        reply = payload.get("reply")
        if not isinstance(reply, Mapping):
            raise ValueError("Slack interrupt delivery requires reply data")
        return cls(
            acknowledgement=acknowledgement,
            reply=SlackInterruptReply.from_payload(reply),
        )


def interrupt_delivery_from_inbound(
    inbound: Mapping[str, Any],
) -> SlackInterruptDeliveryDirective | None:
    outcome = inbound.get("ilo_outcome")
    if not isinstance(outcome, Mapping):
        return None
    directive = outcome.get("delivery_directive")
    if not isinstance(directive, Mapping):
        return None
    return SlackInterruptDeliveryDirective.from_payload(directive)


def should_add_reflex_ack(
    directive: SlackInterruptDeliveryDirective | None,
    *,
    default: bool,
) -> bool:
    if directive is None:
        return default
    return directive.acknowledgement is SlackAcknowledgementMode.EYES_REACTION


def _interrupt_client_msg_id(idempotency_key: str) -> str:
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"illospace:slack-interrupt:{idempotency_key}",
        )
    )


async def post_interrupt_reply(
    bot_token: str,
    directive: SlackInterruptDeliveryDirective,
    *,
    client: Any | None = None,
) -> Mapping[str, Any]:
    reply = directive.reply
    if not reply.channel_id:
        raise ValueError("Slack interrupt reply requires a channel id")
    active_client = client or SlackWebClient(bot_token)
    return await active_client.post_message(
        channel=reply.channel_id,
        text=reply.text,
        thread_ts=reply.thread_ts,
        client_msg_id=_interrupt_client_msg_id(reply.idempotency_key),
    )


__all__ = [
    "SlackAcknowledgementMode",
    "SlackInterruptDeliveryDirective",
    "SlackInterruptReply",
    "interrupt_delivery_from_inbound",
    "post_interrupt_reply",
    "should_add_reflex_ack",
]
