"""Failure handling shared by interactive Slack reply runs."""

from __future__ import annotations

from collections.abc import Mapping

from brain.platform.integrations.providers import is_transient_transport_disconnect


INTERACTIVE_SLACK_ORIGINS = frozenset({"slack_teammate", "slack_channel_monitor"})
INTERACTIVE_TRANSPORT_FALLBACK_MESSAGE = (
    "⚠️ I dropped that — my model connection cut out mid-reply. "
    "Re-send it and I'll pick it back up."
)


def is_interactive_slack_reply_context(*contexts: Mapping | None) -> bool:
    """Recognize a Slack reply surface through nested provenance and surface policy."""

    pending = [context for context in contexts if isinstance(context, Mapping)]
    seen: set[int] = set()
    has_interactive_origin = False
    targets_slack = False
    is_headless = False
    while pending:
        context = pending.pop()
        identity = id(context)
        if identity in seen:
            continue
        seen.add(identity)
        if str(context.get("origin") or "").strip().lower() in INTERACTIVE_SLACK_ORIGINS:
            has_interactive_origin = True
        if str(context.get("final_answer_target_surface") or "").strip().lower() == "slack":
            targets_slack = True
        if (
            bool(context.get("headless"))
            or str(context.get("final_answer_target_surface") or "").strip().lower()
            == "headless"
        ):
            is_headless = True
        for key in ("execution_provenance", "target_ref"):
            nested = context.get(key)
            if isinstance(nested, Mapping):
                pending.append(nested)
    return not is_headless and (has_interactive_origin or targets_slack)


def interactive_transport_fallback(
    error: BaseException | str | None,
    *contexts: Mapping | None,
) -> str | None:
    if not is_interactive_slack_reply_context(*contexts):
        return None
    if not is_transient_transport_disconnect(error):
        return None
    return INTERACTIVE_TRANSPORT_FALLBACK_MESSAGE


def is_interactive_transport_fallback(text: str | None) -> bool:
    return str(text or "").strip() == INTERACTIVE_TRANSPORT_FALLBACK_MESSAGE


__all__ = [
    "INTERACTIVE_SLACK_ORIGINS",
    "INTERACTIVE_TRANSPORT_FALLBACK_MESSAGE",
    "interactive_transport_fallback",
    "is_interactive_slack_reply_context",
    "is_interactive_transport_fallback",
]
