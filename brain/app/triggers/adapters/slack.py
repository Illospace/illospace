"""Slack trigger adapter for teammate-style Illo mentions and DMs."""

from __future__ import annotations

from typing import Any, Mapping

from brain.app.api.authorization import PrincipalIdentity
from brain.app.triggers.contracts import IlloTrigger, stable_idempotency_key

SLACK_SURFACE = "slack"
SLACK_REPLY_TOOL = "post_slack_reply"


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _surface(payload: Mapping[str, Any]) -> str:
    explicit = _clean(payload.get("surface"))
    if explicit:
        return explicit
    channel_type = _clean(payload.get("channel_type"))
    thread_ts = _clean(payload.get("thread_ts"))
    message_ts = _clean(payload.get("message_ts"))
    if channel_type == "im":
        return "slack_dm"
    if thread_ts and message_ts and thread_ts != message_ts:
        return "slack_thread"
    return "slack_channel"


def _response_target(payload: Mapping[str, Any]) -> dict[str, Any]:
    existing = payload.get("response_target")
    if isinstance(existing, Mapping):
        return {
            "channel_id": _clean(existing.get("channel_id") or payload.get("channel_id")),
            "thread_ts": existing.get("thread_ts"),
            "visibility": _clean(existing.get("visibility")) or "public",
        }
    channel_type = _clean(payload.get("channel_type"))
    message_ts = _clean(payload.get("message_ts"))
    thread_ts = _clean(payload.get("thread_ts") or message_ts)
    return {
        "channel_id": _clean(payload.get("channel_id")),
        "thread_ts": None if channel_type == "im" and thread_ts == message_ts else thread_ts,
        "visibility": "public",
    }


def _event_type(payload: Mapping[str, Any]) -> str:
    origin = _clean(payload.get("origin"))
    if origin in {"slack.app_mention", "slack.direct_message"}:
        return origin
    kind = _clean(payload.get("event_kind"))
    if kind == "direct_message":
        return "slack.direct_message"
    return "slack.app_mention"


def _slack_trigger(payload: Mapping[str, Any]) -> dict[str, Any]:
    response_target = _response_target(payload)
    return {
        "team_id": _clean(payload.get("team_id")),
        "enterprise_id": _clean(payload.get("enterprise_id")) or None,
        "channel_id": _clean(payload.get("channel_id")),
        "channel_type": _clean(payload.get("channel_type")),
        "message_ts": _clean(payload.get("message_ts")),
        "thread_ts": _clean(payload.get("thread_ts") or payload.get("message_ts")),
        "slack_user_id": _clean(payload.get("slack_user_id")),
        "bot_user_id": _clean(payload.get("bot_user_id")) or None,
        "text": _clean(payload.get("text"))[:4000],
        "permalink": _clean(payload.get("permalink")) or None,
        "surface": _surface(payload),
        "response_target": response_target,
    }


def _target(slack_trigger: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "kind": "slack_message",
        "team_id": slack_trigger.get("team_id"),
        "channel_id": slack_trigger.get("channel_id"),
        "channel_type": slack_trigger.get("channel_type"),
        "message_ts": slack_trigger.get("message_ts"),
        "thread_ts": slack_trigger.get("thread_ts"),
        "slack_user_id": slack_trigger.get("slack_user_id"),
        "surface": slack_trigger.get("surface"),
    }


def _run_message(payload: Mapping[str, Any], slack_trigger: Mapping[str, Any]) -> str:
    text = _clean(payload.get("text"))[:2000]
    surface = _clean(slack_trigger.get("surface"))
    lines = [
        "A teammate invoked Illo from Slack.",
        "Slack is the triggering conversation surface; use normal Illospace tools for the work.",
        f"After acting, reply in Slack with {SLACK_REPLY_TOOL} when a visible response fits.",
        "Use read_slack_conversation if more Slack context is needed.",
        "",
        f"Slack surface: {surface}",
        f"Team: {slack_trigger.get('team_id')}",
        f"Channel: {slack_trigger.get('channel_id')}",
        f"Message ts: {slack_trigger.get('message_ts')}",
        f"Slack user: {slack_trigger.get('slack_user_id')}",
    ]
    if slack_trigger.get("permalink"):
        lines.append(f"Permalink: {slack_trigger.get('permalink')}")
    lines.extend(["", f"Triggering Slack message: {text}"])
    return "\n".join(lines)


def build_slack_message_trigger(
    *,
    org_id: str,
    authority_user_id: str,
    payload: Mapping[str, Any],
    inbound_event_id: str | None = None,
    connection_id: str | None = None,
    idempotency_key: str | None = None,
    priority: int = 0,
) -> IlloTrigger:
    """Normalize a Slack mention or DM into an Illo-native trigger.

    The self-hosted MVP is permissive: unmapped Slack users run under the Slack
    connection authority while Slack provenance remains explicit in metadata.
    """

    slack_payload = dict(payload or {})
    trigger = _slack_trigger(slack_payload)
    event_type = _event_type(slack_payload)
    target = _target(trigger)
    actor = PrincipalIdentity(
        id=str(authority_user_id),
        principal_type="external_slack_user",
        role="member",
        name=f"Slack user {trigger.get('slack_user_id')}",
        org_id=str(org_id),
        metadata={
            "auth_source": "slack_connection_authority",
            "slack_user_id": trigger.get("slack_user_id"),
            "slack_team_id": trigger.get("team_id"),
            **({"connection_id": connection_id} if connection_id else {}),
        },
    )
    metadata = {
        "origin": "slack_teammate",
        "originating_surface": SLACK_SURFACE,
        "triggering_surface": SLACK_SURFACE,
        "source_surface": SLACK_SURFACE,
        "required_response_tool": SLACK_REPLY_TOOL,
        "final_answer_target_surface": SLACK_SURFACE,
        "slack_trigger": trigger,
        "slack_connection_id": connection_id,
    }
    if inbound_event_id:
        metadata["inbound_event"] = {
            "event_id": inbound_event_id,
            "origin": event_type,
            "kind": "slack_message",
            "connection_id": connection_id,
        }
    payload_for_trigger = {
        "slack": trigger,
        "thread_message": _clean(slack_payload.get("text"))[:2000],
        "run_message": _run_message(slack_payload, trigger),
        "metadata": metadata,
        "priority": int(priority),
        "user_id": str(authority_user_id),
    }
    key = idempotency_key or stable_idempotency_key(
        source="slack",
        event_type=event_type,
        org_id=str(org_id),
        target=target,
        payload={
            "message_ts": trigger.get("message_ts"),
            "priority": int(priority),
        },
    )
    return IlloTrigger(
        source="slack",
        event_type=event_type,
        actor=actor,
        org_id=str(org_id),
        target=target,
        payload=payload_for_trigger,
        idempotency_key=key,
        policy={
            "route": "run",
            "run_event": event_type.split(".", 1)[-1],
            "priority": int(priority),
            "auth_path": "slack_connection",
        },
    )
