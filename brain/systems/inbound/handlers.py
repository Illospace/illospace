"""Public contracts and registry for specialized inbound envelope handlers."""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, cast

from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.inbound import (
    InboundEventRow,
    InboundSourcePolicyRow,
)


@dataclass(frozen=True)
class InboundHandlerContext:
    """Resolved connection authority exposed to registered envelope handlers."""

    connection_id: str
    org_id: str
    owner_user_id: str | None
    token_id: str | None
    scopes: frozenset[str] | None
    display_name: str | None
    source_kind: str | None


@dataclass(frozen=True)
class InboundCompletion:
    """The typed completion requested by an inbound envelope handler."""

    status: str
    action_type: str
    action_result: Mapping[str, Any]
    confidence: float | None
    policy: InboundSourcePolicyRow | None = None
    error: str | None = None
    target: Mapping[str, Any] = field(default_factory=dict)
    tool_use: Mapping[str, Any] = field(default_factory=dict)
    reasoning_summary: str | None = None
    reusable_pattern_candidate: Mapping[str, Any] = field(default_factory=dict)


class InboundEventCompleter(Protocol):
    """Bound receipt/finalization callback supplied by the inbound service."""

    async def __call__(self, completion: InboundCompletion, /) -> dict[str, Any]: ...


class InboundEnvelopeHandler(Protocol):
    """Callable contract for one registered inbound envelope kind."""

    async def __call__(
        self,
        session: AsyncSession,
        *,
        context: InboundHandlerContext,
        event: InboundEventRow,
        normalized: Mapping[str, Any],
        complete: InboundEventCompleter,
    ) -> dict[str, Any]: ...


InboundHandlerReference = InboundEnvelopeHandler | str

_HANDLERS: dict[str, InboundHandlerReference] = {}


def register_inbound_envelope_handler(
    kind: str,
    handler: InboundHandlerReference,
) -> InboundHandlerReference | None:
    """Register a handler callable or lazy ``module:function`` reference."""

    normalized_kind = str(kind or "").strip()
    if not normalized_kind:
        raise ValueError("Inbound envelope handler kind is required")
    if isinstance(handler, str) and ":" not in handler:
        raise ValueError("Inbound envelope handler path must be module:function")
    previous = _HANDLERS.get(normalized_kind)
    _HANDLERS[normalized_kind] = handler
    return previous


def unregister_inbound_envelope_handler(kind: str) -> InboundHandlerReference | None:
    """Remove a registration, primarily for isolated extension tests."""

    return _HANDLERS.pop(str(kind or "").strip(), None)


def resolve_inbound_envelope_handler(kind: str) -> InboundEnvelopeHandler | None:
    """Resolve a registered handler without importing unrelated surface modules."""

    reference = _HANDLERS.get(str(kind or "").strip())
    if reference is None:
        return None
    if not isinstance(reference, str):
        return reference
    module_name, function_name = reference.split(":", 1)
    handler = getattr(importlib.import_module(module_name), function_name)
    return cast(InboundEnvelopeHandler, handler)


register_inbound_envelope_handler(
    "app_report",
    "brain.systems.app_report.inbound:process_app_report_envelope",
)
register_inbound_envelope_handler(
    "slack_message",
    "brain.systems.slack.inbound:process_slack_message_envelope",
)
register_inbound_envelope_handler(
    "meeting_transcript",
    "brain.systems.meetings.inbound:process_meeting_transcript_envelope",
)


__all__ = [
    "InboundCompletion",
    "InboundEnvelopeHandler",
    "InboundEventCompleter",
    "InboundHandlerContext",
    "register_inbound_envelope_handler",
    "resolve_inbound_envelope_handler",
    "unregister_inbound_envelope_handler",
]
