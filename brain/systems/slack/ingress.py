"""Normalize Slack Socket Mode events into Illospace inbound envelopes."""

from __future__ import annotations

from typing import Any, Mapping

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


def _event_origin(event: Mapping[str, Any], *, bot_user_id: str | None) -> str | None:
    event_type = str(event.get("type") or "")
    subtype = str(event.get("subtype") or "")
    channel_type = str(event.get("channel_type") or "")
    text = str(event.get("text") or "")
    if event.get("bot_id") or subtype in _IGNORED_MESSAGE_SUBTYPES:
        return None
    if event_type == "app_mention":
        return "slack.app_mention"
    if event_type != "message":
        return None
    if channel_type == "im":
        return "slack.direct_message"
    if bot_user_id and f"<@{bot_user_id}>" in text:
        return "slack.app_mention"
    return None


def _event_kind(origin: str) -> str:
    if origin == "slack.direct_message":
        return "direct_message"
    return "mention"


def _surface(channel_type: str, thread_ts: str, message_ts: str) -> str:
    if channel_type == "im":
        return "slack_dm"
    if thread_ts and thread_ts != message_ts:
        return "slack_thread"
    return "slack_channel"


def _response_thread_ts(channel_type: str, thread_ts: str, message_ts: str) -> str | None:
    if channel_type == "im" and thread_ts == message_ts:
        return None
    return thread_ts or message_ts or None


def normalize_slack_socket_event(
    socket_payload: Mapping[str, Any],
    *,
    bot_user_id: str | None = None,
) -> dict[str, Any] | None:
    """Return a shared inbound envelope for actionable Slack Socket Mode events.

    Only explicit mentions and DMs are actionable in the self-hosted MVP. Other
    channel messages may be visible to the bot, but they are not invitations for
    Illo to participate.
    """

    payload = _socket_payload(socket_payload)
    event = _slack_event(socket_payload)
    resolved_bot_user_id = _bot_user_id(socket_payload, bot_user_id)
    origin = _event_origin(event, bot_user_id=resolved_bot_user_id)
    if origin is None:
        return None

    team_id = str(payload.get("team_id") or event.get("team") or "").strip()
    enterprise_id = str(payload.get("enterprise_id") or event.get("enterprise") or "").strip() or None
    channel_id = str(event.get("channel") or "").strip()
    channel_type = str(event.get("channel_type") or "").strip()
    message_ts = str(event.get("ts") or event.get("event_ts") or "").strip()
    thread_ts = str(event.get("thread_ts") or message_ts).strip()
    slack_user_id = str(event.get("user") or "").strip()
    event_id = str(payload.get("event_id") or "").strip()
    idempotency_key = f"slack:{team_id}:{event_id}" if event_id else f"slack:{team_id}:{channel_id}:{message_ts}"
    surface = _surface(channel_type, thread_ts, message_ts)
    response_target = {
        "channel_id": channel_id,
        "thread_ts": _response_thread_ts(channel_type, thread_ts, message_ts),
        "visibility": "public",
    }
    permalink = str(event.get("permalink") or "").strip() or None
    normalized_payload = {
        "event_kind": _event_kind(origin),
        "origin": origin,
        "team_id": team_id,
        "enterprise_id": enterprise_id,
        "channel_id": channel_id,
        "channel_type": channel_type,
        "message_ts": message_ts,
        "thread_ts": thread_ts,
        "slack_user_id": slack_user_id,
        "bot_user_id": resolved_bot_user_id,
        "text": _bounded_text(event.get("text")),
        "permalink": permalink,
        "event_id": event_id or None,
        "event_time": payload.get("event_time"),
        "api_app_id": payload.get("api_app_id"),
        "surface": surface,
        "response_target": response_target,
    }
    return {
        "kind": SLACK_MESSAGE_ENVELOPE_KIND,
        "origin": origin,
        "payload": normalized_payload,
        "summary": _bounded_text(event.get("text"), limit=500),
        "hints": {
            "surface": {
                "kind": "slack",
                "surface": surface,
                "team_id": team_id,
                "channel_id": channel_id,
                "channel_type": channel_type,
                "message_ts": message_ts,
                "thread_ts": thread_ts,
            },
            "response_target": response_target,
        },
        "desired_outcome": "Illo should handle the request and reply in Slack when a visible response fits.",
        "idempotency_key": idempotency_key[:160],
    }
