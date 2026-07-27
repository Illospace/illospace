"""Slack trigger shaping shared by app adapters and Slack systems."""

from __future__ import annotations

from typing import Any, Mapping

from brain.systems.personality.person_context import normalize_person_context
from brain.systems.slack.channel_monitor_rendering import (
    durable_preference_guidance,
)
from brain.systems.slack.monitored_intakes import (
    SLACK_CHANNEL_MESSAGE_ORIGIN,
    is_monitored_intake,
    monitored_intake_policy,
    route_monitored_intake,
    slack_response_thread_ts,
)

SLACK_MESSAGE_ENVELOPE_KIND = "slack_message"
SLACK_SURFACE = "slack"
SLACK_REPLY_TOOL = "post_slack_reply"
SLACK_REACTION_TOOL = "react_to_slack_message"


def build_slack_work_intake_payload(
    *,
    org_id: str,
    authority_user_id: str,
    payload: Mapping[str, Any],
    inbound_event_id: str | None = None,
    connection_id: str | None = None,
    idempotency_key: str | None = None,
    priority: int = 0,
    person_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    slack_payload = dict(payload or {})
    verified_person = normalize_person_context(
        person_context,
        verified_user_id=str(authority_user_id),
    )
    trigger = slack_trigger(slack_payload)
    event_type = slack_event_type(slack_payload)
    target = slack_target(trigger)
    monitored_route = route_monitored_intake(slack_payload, trigger)
    metadata = {
        "origin": (
            monitored_route.metadata_origin
            if monitored_route is not None
            else "slack_teammate"
        ),
        "originating_surface": SLACK_SURFACE,
        "triggering_surface": SLACK_SURFACE,
        "source_surface": SLACK_SURFACE,
        "slack_trigger": trigger,
        "slack_connection_id": connection_id,
    }
    if monitored_route is not None:
        metadata.update(monitored_route.metadata)
        run_message = monitored_route.run_message
    else:
        metadata["required_response_tool"] = SLACK_REPLY_TOOL
        metadata["alternative_response_tools"] = [SLACK_REACTION_TOOL]
        metadata["final_answer_target_surface"] = SLACK_SURFACE
        run_message = slack_run_message(slack_payload, trigger)
    if inbound_event_id:
        metadata["inbound_event"] = {
            "event_id": inbound_event_id,
            "origin": event_type,
            "kind": SLACK_MESSAGE_ENVELOPE_KIND,
            "connection_id": connection_id,
        }
    if verified_person:
        metadata["person_context"] = verified_person
    actor_name = f"Slack user {trigger.get('slack_user_id')}"
    return {
        "source": "slack",
        "event_type": event_type,
        "actor": {
            "id": str(authority_user_id),
            "principal_type": "external_slack_user",
            "role": "member",
            "name": actor_name,
            "org_id": str(org_id),
            "metadata": {
                "auth_source": "slack_connection_authority",
                "slack_user_id": trigger.get("slack_user_id"),
                "slack_team_id": trigger.get("team_id"),
                **({"connection_id": connection_id} if connection_id else {}),
            },
        },
        "org_id": str(org_id),
        "target": target,
        "payload": {
            "slack": trigger,
            "thread_message": _clean(slack_payload.get("text"))[:2000],
            "run_message": run_message,
            "metadata": metadata,
            "priority": int(priority),
            "user_id": str(authority_user_id),
        },
        "idempotency_key": idempotency_key,
        "policy": {
            "route": "run",
            "run_event": event_type.split(".", 1)[-1],
            "priority": int(priority),
            "auth_path": "slack_connection",
        },
    }


def slack_trigger(payload: Mapping[str, Any]) -> dict[str, Any]:
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
        "surface": slack_surface(payload),
        "response_target": slack_response_target(payload),
    }


def slack_target(slack_trigger_payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "kind": SLACK_MESSAGE_ENVELOPE_KIND,
        "team_id": slack_trigger_payload.get("team_id"),
        "channel_id": slack_trigger_payload.get("channel_id"),
        "channel_type": slack_trigger_payload.get("channel_type"),
        "message_ts": slack_trigger_payload.get("message_ts"),
        "thread_ts": slack_trigger_payload.get("thread_ts"),
        "slack_user_id": slack_trigger_payload.get("slack_user_id"),
        "surface": slack_trigger_payload.get("surface"),
    }


def slack_event_type(payload: Mapping[str, Any]) -> str:
    origin = _clean(payload.get("origin"))
    monitored_policy = monitored_intake_policy(payload)
    if origin in {
        "slack.app_mention",
        "slack.direct_message",
    }:
        return origin
    if monitored_policy is not None:
        return monitored_policy.origin
    event_kind = _clean(payload.get("event_kind"))
    if event_kind == "direct_message":
        return "slack.direct_message"
    return "slack.app_mention"


def slack_surface(payload: Mapping[str, Any]) -> str:
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


def slack_response_target(payload: Mapping[str, Any]) -> dict[str, Any]:
    existing = payload.get("response_target")
    channel_type = _clean(payload.get("channel_type"))
    message_ts = _clean(payload.get("message_ts"))
    is_monitored_channel = is_monitored_intake(payload)
    if isinstance(existing, Mapping):
        thread_ts = slack_response_thread_ts(
            channel_type,
            _clean(existing.get("thread_ts")),
            message_ts,
            is_monitored=is_monitored_channel,
        )
        return {
            "channel_id": _clean(existing.get("channel_id") or payload.get("channel_id")),
            "thread_ts": thread_ts,
            "visibility": _clean(existing.get("visibility")) or "public",
        }
    thread_ts = _clean(payload.get("thread_ts") or message_ts)
    return {
        "channel_id": _clean(payload.get("channel_id")),
        "thread_ts": slack_response_thread_ts(
            channel_type,
            thread_ts,
            message_ts,
            is_monitored=is_monitored_channel,
        ),
        "visibility": "public",
    }


def slack_run_message(payload: Mapping[str, Any], slack_trigger_payload: Mapping[str, Any]) -> str:
    text = _clean(payload.get("text"))[:2000]
    surface = _clean(slack_trigger_payload.get("surface"))
    response_target = slack_trigger_payload.get("response_target")
    response_target = response_target if isinstance(response_target, Mapping) else {}
    reply_thread_ts = _clean(response_target.get("thread_ts"))
    channel_type = _clean(slack_trigger_payload.get("channel_type"))
    if channel_type == "im":
        reply_mode = "Slack DM normal message; do not pass thread_ts."
    elif reply_thread_ts:
        reply_mode = f"Slack thread reply using thread_ts {reply_thread_ts}."
    else:
        reply_mode = "Top-level Slack channel message; do not pass thread_ts."
    lines = [
        "A teammate invoked Illo from Slack.",
        "Slack is the triggering conversation surface; use normal Illospace tools for the work.",
        "Decide whether this is a simple Slack reply or work that should be delegated into Cortex/worker runs.",
        f"For simple requests, reply in Slack with {SLACK_REPLY_TOOL}.",
        (
            f"For a purely social acknowledgement that needs no answer, use {SLACK_REACTION_TOOL} "
            "instead of posting text. Use one fitting reaction. Do not react and reply unless "
            "each action serves a distinct purpose."
        ),
        (
            "Questions, requests, corrections, incidents, and sensitive messages need a clear "
            f"text response with {SLACK_REPLY_TOOL}; a reaction never replaces the answer."
        ),
        (
            "For long-running work, make the delegation durable with manage_idea or spawn_worker, "
            f"then send a model-authored Slack update with {SLACK_REPLY_TOOL}."
        ),
        "Only share Cortex Thread links returned by tools as thread_url; never build a URL from Slack ids or run ids.",
        "Use read_slack_conversation if more Slack context is needed.",
        f"Default Slack reply target: {reply_mode}",
        "",
        durable_preference_guidance(),
        "",
        f"Slack surface: {surface}",
        f"Team: {slack_trigger_payload.get('team_id')}",
        f"Channel: {slack_trigger_payload.get('channel_id')}",
        f"Message ts: {slack_trigger_payload.get('message_ts')}",
        f"Slack user: {slack_trigger_payload.get('slack_user_id')}",
    ]
    if slack_trigger_payload.get("permalink"):
        lines.append(f"Permalink: {slack_trigger_payload.get('permalink')}")
    lines.extend(["", f"Triggering Slack message: {text}"])
    return "\n".join(lines)


def _clean(value: Any) -> str:
    return str(value or "").strip()


__all__ = [
    "SLACK_CHANNEL_MESSAGE_ORIGIN",
    "SLACK_MESSAGE_ENVELOPE_KIND",
    "SLACK_REPLY_TOOL",
    "SLACK_REACTION_TOOL",
    "SLACK_SURFACE",
    "build_slack_work_intake_payload",
    "slack_event_type",
    "slack_response_target",
    "slack_run_message",
    "slack_surface",
    "slack_target",
    "slack_trigger",
]
