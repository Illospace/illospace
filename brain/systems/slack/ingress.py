"""Normalize Slack Socket Mode events into Illospace inbound envelopes."""

from __future__ import annotations

import logging
from typing import Any, Mapping

from brain.systems.slack.monitored_intakes import (
    MonitoredIntakeMatch,
    enrich_monitored_intake_payload,
    recognize_monitored_intake,
    slack_response_thread_ts,
    typed_monitored_intake_origins,
    visible_slack_content,
)

logger = logging.getLogger(__name__)

SLACK_MESSAGE_ENVELOPE_KIND = "slack_message"
MAX_SLACK_TEXT_CHARS = 4000

_IGNORED_MESSAGE_SUBTYPES = {
    "bot_message",
    "channel_join",
    "channel_leave",
    "message_changed",
    "message_deleted",
    "message_replied",
}


def _bounded_text(value: Any, *, limit: int = MAX_SLACK_TEXT_CHARS) -> str:
    return str(value or "")[:limit]


def _socket_payload(socket_payload: Mapping[str, Any]) -> dict[str, Any]:
    data = dict(socket_payload or {})
    payload = data.get("payload")
    return dict(payload or data) if isinstance(payload, Mapping) else data


def _slack_event(socket_payload: Mapping[str, Any]) -> dict[str, Any]:
    payload = _socket_payload(socket_payload)
    event = payload.get("event")
    return dict(event or {}) if isinstance(event, Mapping) else {}


def _bot_user_id(socket_payload: Mapping[str, Any], explicit_bot_user_id: str | None) -> str | None:
    if explicit_bot_user_id:
        return str(explicit_bot_user_id)
    payload = _socket_payload(socket_payload)
    for authorization in payload.get("authorizations") or []:
        if not isinstance(authorization, Mapping):
            continue
        if authorization.get("is_bot") and authorization.get("user_id"):
            return str(authorization["user_id"])
    return None


def _is_own_slack_message(
    event: Mapping[str, Any],
    *,
    bot_user_id: str | None,
    api_app_id: str | None,
) -> bool:
    """Return True when a message was authored by Illo's own Slack app.

    Guards against Illo reacting to or triaging its own posts, which would loop
    on a monitored channel.
    """

    if bot_user_id and str(event.get("user") or "").strip() == str(bot_user_id):
        return True
    event_app_id = str(event.get("app_id") or "").strip()
    if event_app_id and api_app_id and event_app_id == str(api_app_id).strip():
        return True
    return False


def _event_origin(
    event: Mapping[str, Any],
    *,
    bot_user_id: str | None,
    api_app_id: str | None = None,
    monitored_channels: frozenset[str] | set[str] | None = None,
    monitored_intake: MonitoredIntakeMatch | None = None,
) -> str | None:
    event_type = str(event.get("type") or "")
    subtype = str(event.get("subtype") or "")
    channel_type = str(event.get("channel_type") or "")
    text = str(event.get("text") or "")
    channel_id = str(event.get("channel") or "").strip()
    if (
        subtype in _IGNORED_MESSAGE_SUBTYPES
        and (
            monitored_intake is None
            or subtype
            not in monitored_intake.policy.allowed_ignored_subtypes
        )
    ):
        return None
    if _is_own_slack_message(event, bot_user_id=bot_user_id, api_app_id=api_app_id):
        return None
    is_bot = bool(event.get("bot_id"))
    # Direct human invitations to participate: explicit mentions and DMs.
    if not is_bot:
        if event_type == "app_mention":
            return "slack.app_mention"
        if event_type == "message" and channel_type == "im":
            return "slack.direct_message"
        if event_type == "message" and bot_user_id and f"<@{bot_user_id}>" in text:
            return "slack.app_mention"
    # Passive monitoring: every message in an explicitly monitored channel,
    # including third-party bots (Sentry, Rollbar) posting automated alerts.
    if (
        event_type == "message"
        and channel_type != "im"
        and channel_id
        and monitored_channels
        and channel_id in monitored_channels
    ):
        if monitored_intake is None:
            return None
        return monitored_intake.policy.origin
    return None


def _event_kind(
    origin: str,
    monitored_intake: MonitoredIntakeMatch | None,
) -> str:
    if origin == "slack.direct_message":
        return "direct_message"
    if (
        monitored_intake is not None
        and origin == monitored_intake.policy.origin
    ):
        return monitored_intake.policy.event_kind
    return "mention"


def _surface(channel_type: str, thread_ts: str, message_ts: str) -> str:
    if channel_type == "im":
        return "slack_dm"
    if thread_ts and thread_ts != message_ts:
        return "slack_thread"
    return "slack_channel"


def normalize_slack_socket_event(
    socket_payload: Mapping[str, Any],
    *,
    bot_user_id: str | None = None,
    monitored_channels: frozenset[str] | set[str] | list[str] | None = None,
    disabled_intakes: frozenset[str] | set[str] | list[str] | None = None,
) -> dict[str, Any] | None:
    """Return a shared inbound envelope for actionable Slack Socket Mode events.

    Explicit mentions and DMs are always actionable. In addition, every message
    posted to an explicitly *monitored* channel (``monitored_channels``) is
    admitted as a ``slack.channel_message`` so Illo can observe and triage the
    channel; other channel messages remain non-actionable.
    """

    payload = _socket_payload(socket_payload)
    event = _slack_event(socket_payload)
    visible_content = visible_slack_content(event)
    monitored_intake = recognize_monitored_intake(event, visible_content)
    disabled = frozenset(
        str(intake).strip()
        for intake in (disabled_intakes or ())
        if str(intake).strip()
    )
    resolved_bot_user_id = _bot_user_id(socket_payload, bot_user_id)
    api_app_id = str(payload.get("api_app_id") or "").strip() or None
    monitored = frozenset(
        str(channel).strip() for channel in (monitored_channels or ()) if str(channel).strip()
    )
    origin = _event_origin(
        event,
        bot_user_id=resolved_bot_user_id,
        api_app_id=api_app_id,
        monitored_channels=monitored,
        monitored_intake=monitored_intake,
    )
    if origin is None:
        return None
    # A disabled intake silences only the passive monitored-channel lane —
    # explicit mentions and DMs resolve their own origin above and stay
    # actionable even when their text happens to decode as a typed intake.
    if (
        origin == monitored_intake.policy.origin
        and origin in typed_monitored_intake_origins()
        and origin in disabled
    ):
        logger.info(
            "slack_monitored_intake_disabled: intake=%s channel_id=%s message_ts=%s",
            origin,
            str(event.get("channel") or "").strip(),
            str(event.get("ts") or event.get("event_ts") or "").strip(),
        )
        return None
    message_text = (
        monitored_intake.text
        if origin == monitored_intake.policy.origin
        else visible_content.message_text
    )

    team_id = str(payload.get("team_id") or event.get("team") or "").strip()
    enterprise_id = str(payload.get("enterprise_id") or event.get("enterprise") or "").strip() or None
    channel_id = str(event.get("channel") or "").strip()
    channel_name = str(event.get("channel_name") or event.get("channel_name_normalized") or "").strip() or None
    channel_type = str(event.get("channel_type") or "").strip()
    message_ts = str(event.get("ts") or event.get("event_ts") or "").strip()
    thread_ts = str(event.get("thread_ts") or message_ts).strip()
    slack_user_id = str(event.get("user") or "").strip()
    event_id = str(payload.get("event_id") or "").strip()
    # Slack may deliver both an app_mention event and a message.* event for the
    # same user-visible mention. Use the Slack message identity, not Slack's
    # per-event id, so one Slack message admits at most one Illo run.
    if channel_id and message_ts:
        idempotency_key = f"slack:{team_id}:{channel_id}:{message_ts}"
    else:
        idempotency_key = f"slack:{team_id}:{event_id}"
    surface = _surface(channel_type, thread_ts, message_ts)
    response_target = {
        "channel_id": channel_id,
        "thread_ts": slack_response_thread_ts(
            channel_type,
            thread_ts,
            message_ts,
            is_monitored=origin == monitored_intake.policy.origin,
        ),
        "visibility": "public",
    }
    permalink = str(event.get("permalink") or "").strip() or None
    normalized_payload = {
        "event_kind": _event_kind(origin, monitored_intake),
        "origin": origin,
        "team_id": team_id,
        "enterprise_id": enterprise_id,
        "channel_id": channel_id,
        "channel_name": channel_name,
        "channel_type": channel_type,
        "message_ts": message_ts,
        "thread_ts": thread_ts,
        "slack_user_id": slack_user_id,
        "bot_user_id": resolved_bot_user_id,
        "text": message_text,
        "permalink": permalink,
        "event_id": event_id or None,
        "event_time": payload.get("event_time"),
        "api_app_id": payload.get("api_app_id"),
        "surface": surface,
        "response_target": response_target,
    }
    if origin == monitored_intake.policy.origin:
        enrich_monitored_intake_payload(
            normalized_payload,
            monitored_intake,
        )
    return {
        "kind": SLACK_MESSAGE_ENVELOPE_KIND,
        "origin": origin,
        "payload": normalized_payload,
        "summary": _bounded_text(message_text, limit=500),
        "hints": {
            "surface": {
                "kind": "slack",
                "surface": surface,
                "team_id": team_id,
                "channel_id": channel_id,
                "channel_name": channel_name,
                "channel_type": channel_type,
                "message_ts": message_ts,
                "thread_ts": thread_ts,
            },
            "response_target": response_target,
        },
        "desired_outcome": "Illo should handle the request and reply in Slack when a visible response fits.",
        "idempotency_key": idempotency_key[:160],
    }
