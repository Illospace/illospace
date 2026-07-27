"""Typed policy contract for delayed, externally answered obligations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from typing import Any, Mapping, Protocol


class ObligationNoticeRenderer(Protocol):
    """Render the public reminder owned by an obligation type."""

    def render(self) -> str: ...


@dataclass(frozen=True, slots=True)
class StaticObligationNoticeRenderer:
    """Rehydrate already-rendered notice copy from durable run metadata."""

    text: str

    def render(self) -> str:
        return self.text


@dataclass(frozen=True, slots=True)
class ObligationAnswerer:
    """The person whose reply can settle an externally owned obligation."""

    name: str
    slack_user_id: str
    user_id: str | None = None

    def to_metadata(self) -> dict[str, str | None]:
        return {
            "name": self.name,
            "slack_user_id": self.slack_user_id,
            "user_id": self.user_id,
        }

    @classmethod
    def from_metadata(cls, value: Mapping[str, Any]) -> ObligationAnswerer:
        name = _clean(value.get("name"))
        slack_user_id = _clean(value.get("slack_user_id"))
        if not name or not slack_user_id:
            raise ValueError("obligation answerers require a name and Slack user id")
        return cls(
            name=name,
            slack_user_id=slack_user_id,
            user_id=_clean(value.get("user_id")) or None,
        )


@dataclass(frozen=True, slots=True)
class InboundSlackReply:
    """Normalized inbound facts evaluated by a settlement policy."""

    slack_user_id: str
    message_ts: str
    text: str


class ObligationSettlementPolicy(StrEnum):
    """Supported generic ways an external obligation can be settled."""

    ANSWERER_SLACK_REPLY = "answerer_slack_reply"

    def matches(
        self,
        *,
        answerer: ObligationAnswerer,
        reply: InboundSlackReply,
    ) -> bool:
        if self is ObligationSettlementPolicy.ANSWERER_SLACK_REPLY:
            return bool(
                reply.message_ts
                and reply.slack_user_id == answerer.slack_user_id
            )
        return False


@dataclass(frozen=True, slots=True)
class ObligationSpec:
    """Owner-neutral policy for one delayed answer obligation.

    The typed condition owns the outbox key. The remaining fields own who may
    answer, when the notice becomes due, what it says, and how an inbound event
    settles it. ``to_metadata`` stores the durable policy beside the originating
    run, so the existing obligation tables need no type-specific columns.
    """

    condition: str
    answerer: ObligationAnswerer
    notice_after: timedelta
    renderer: ObligationNoticeRenderer
    settlement_policy: ObligationSettlementPolicy

    def __post_init__(self) -> None:
        if not _clean(self.condition):
            raise ValueError("obligation specs require a condition")
        if self.notice_after < timedelta(0):
            raise ValueError("obligation notice delay cannot be negative")
        if not _clean(self.renderer.render()):
            raise ValueError("obligation notice renderer produced empty copy")

    def to_metadata(self) -> dict[str, Any]:
        return {
            "condition": self.condition,
            "answerer": self.answerer.to_metadata(),
            "notice_after_seconds": int(self.notice_after.total_seconds()),
            "notice_text": self.renderer.render(),
            "settlement_policy": self.settlement_policy.value,
        }

    @classmethod
    def from_metadata(cls, value: Mapping[str, Any]) -> ObligationSpec:
        condition = _clean(value.get("condition"))
        answerer = value.get("answerer")
        notice_text = str(value.get("notice_text") or "")
        try:
            notice_after_seconds = int(value.get("notice_after_seconds") or 0)
            settlement_policy = ObligationSettlementPolicy(
                _clean(value.get("settlement_policy"))
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid obligation spec metadata") from exc
        if not isinstance(answerer, Mapping):
            raise ValueError("obligation spec metadata requires an answerer")
        return cls(
            condition=condition,
            answerer=ObligationAnswerer.from_metadata(answerer),
            notice_after=timedelta(seconds=notice_after_seconds),
            renderer=StaticObligationNoticeRenderer(notice_text),
            settlement_policy=settlement_policy,
        )

    def settles(self, reply: InboundSlackReply) -> bool:
        return self.settlement_policy.matches(
            answerer=self.answerer,
            reply=reply,
        )


def obligation_spec_from_metadata(value: Any) -> ObligationSpec | None:
    if not isinstance(value, Mapping):
        return None
    try:
        return ObligationSpec.from_metadata(value)
    except (OverflowError, TypeError, ValueError):
        return None


def _clean(value: Any) -> str:
    return str(value or "").strip()


__all__ = [
    "InboundSlackReply",
    "ObligationAnswerer",
    "ObligationNoticeRenderer",
    "ObligationSettlementPolicy",
    "ObligationSpec",
    "StaticObligationNoticeRenderer",
    "obligation_spec_from_metadata",
]
