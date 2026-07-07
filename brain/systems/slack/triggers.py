"""Slack trigger shaping shared by app adapters and Slack systems."""

from __future__ import annotations

from typing import Any, Mapping

SLACK_MESSAGE_ENVELOPE_KIND = "slack_message"
SLACK_SURFACE = "slack"
SLACK_REPLY_TOOL = "post_slack_reply"
SLACK_CHANNEL_MESSAGE_ORIGIN = "slack.channel_message"


def build_slack_work_intake_payload(
    *,
    org_id: str,
    authority_user_id: str,
    payload: Mapping[str, Any],
    inbound_event_id: str | None = None,
    connection_id: str | None = None,
    idempotency_key: str | None = None,
    priority: int = 0,
) -> dict[str, Any]:
    slack_payload = dict(payload or {})
    trigger = slack_trigger(slack_payload)
    event_type = slack_event_type(slack_payload)
    target = slack_target(trigger)
    is_monitor = event_type == SLACK_CHANNEL_MESSAGE_ORIGIN
    metadata = {
        "origin": "slack_channel_monitor" if is_monitor else "slack_teammate",
        "originating_surface": SLACK_SURFACE,
        "triggering_surface": SLACK_SURFACE,
        "source_surface": SLACK_SURFACE,
        "slack_trigger": trigger,
        "slack_connection_id": connection_id,
    }
    if is_monitor:
        # Passive observation of a monitored channel: run headless so the
        # settlement layer never auto-posts the final answer, and do not force a
        # response tool. Illo replies only when it explicitly chooses to via
        # post_slack_reply (e.g. after creating a ticket).
        metadata["slack_monitor"] = True
        metadata["headless"] = True
        metadata["final_answer_target_surface"] = "headless"
        metadata["execution_profile"] = "fast"
        run_message = slack_channel_monitor_message(slack_payload, trigger)
    else:
        metadata["required_response_tool"] = SLACK_REPLY_TOOL
        metadata["final_answer_target_surface"] = SLACK_SURFACE
        run_message = slack_run_message(slack_payload, trigger)
    if inbound_event_id:
        metadata["inbound_event"] = {
            "event_id": inbound_event_id,
            "origin": event_type,
            "kind": SLACK_MESSAGE_ENVELOPE_KIND,
            "connection_id": connection_id,
        }
    return {
        "source": "slack",
        "event_type": event_type,
        "actor": {
            "id": str(authority_user_id),
            "principal_type": "external_slack_user",
            "role": "member",
            "name": f"Slack user {trigger.get('slack_user_id')}",
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
    if origin in {"slack.app_mention", "slack.direct_message", SLACK_CHANNEL_MESSAGE_ORIGIN}:
        return origin
    event_kind = _clean(payload.get("event_kind"))
    if event_kind == "direct_message":
        return "slack.direct_message"
    if event_kind == "channel_message":
        return SLACK_CHANNEL_MESSAGE_ORIGIN
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
    if isinstance(existing, Mapping):
        thread_ts = _response_thread_ts(
            channel_type,
            _clean(existing.get("thread_ts")),
            message_ts,
        )
        return {
            "channel_id": _clean(existing.get("channel_id") or payload.get("channel_id")),
            "thread_ts": thread_ts,
            "visibility": _clean(existing.get("visibility")) or "public",
        }
    thread_ts = _clean(payload.get("thread_ts") or message_ts)
    return {
        "channel_id": _clean(payload.get("channel_id")),
        "thread_ts": _response_thread_ts(channel_type, thread_ts, message_ts),
        "visibility": "public",
    }


def _response_thread_ts(channel_type: str, thread_ts: str, message_ts: str) -> str | None:
    if channel_type == "im":
        return None
    if thread_ts and thread_ts != message_ts:
        return thread_ts
    return None


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
            "For long-running work, make the delegation durable with manage_idea or spawn_worker, "
            f"then send a model-authored Slack update with {SLACK_REPLY_TOOL}."
        ),
        "Only share Cortex Thread links returned by tools as thread_url; never build a URL from Slack ids or run ids.",
        "Use read_slack_conversation if more Slack context is needed.",
        f"Default Slack reply target: {reply_mode}",
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


def slack_channel_monitor_message(
    payload: Mapping[str, Any],
    slack_trigger_payload: Mapping[str, Any],
) -> str:
    """Frame a monitored-channel message as a passive triage decision.

    The message has already been acknowledged with a 👀 reaction at ingest, so
    the run's job is only to decide whether the content is ticket-worthy — and to
    stay silent otherwise.
    """

    text = _clean(payload.get("text"))[:2000]
    channel_id = _clean(slack_trigger_payload.get("channel_id"))
    channel_name = _clean(payload.get("channel_name"))
    channel_label = f"#{channel_name}" if channel_name else f"channel {channel_id}"
    author = _clean(slack_trigger_payload.get("slack_user_id")) or "unknown (may be an app/bot)"
    lines = [
        f"You are passively monitoring Slack {channel_label}. A new message was posted "
        "and has already been acknowledged with a 👀 reaction — do not acknowledge it again.",
        "",
        "Classify this message and act accordingly:",
        "- Casual chatter, or discussion about an existing alert: take NO visible action. Do not reply.",
        "- A genuine automated alert (Sentry, Rollbar, CI) or a user-reported problem that is "
        "ticket-worthy AND the target repo and incident are both clear: open a REAL GitHub issue "
        "with create_github_issue in the correct uwear-ai repo. Load the 'uwear-engineering-triage' "
        "skill (brain_skills then skill_view) first for routing/ownership rules, then optionally "
        "post a brief Slack note with post_slack_reply citing the issue number and URL.",
        "- Ticket-worthy but the repo/incident is unclear, or create_github_issue reports "
        "no write-capable token can reach the repo (no_write_token / 403 / 404): do NOT claim a "
        "GitHub issue was filed. Ask for clarification with post_slack_reply, or record an internal "
        "tracker record + handoff so it is not lost.",
        "- Ambiguous or low-signal: prefer no visible action; the 👀 already confirms you saw it.",
        "",
        "Silence is the correct default. Only use post_slack_reply when you have opened/flagged a "
        "ticket or must surface something important. Use read_slack_conversation "
        "(scope=recent_channel or thread) for more context before deciding. An internal Domain/"
        "tracker record is NOT a GitHub issue — only a successful create_github_issue opens a real "
        "issue; never describe a tracker record as a filed GitHub issue.",
        "",
        f"Channel: {channel_id}" + (f" ({channel_name})" if channel_name else ""),
        f"Team: {slack_trigger_payload.get('team_id')}",
        f"Message ts: {slack_trigger_payload.get('message_ts')}",
        f"Author (Slack id): {author}",
    ]
    if slack_trigger_payload.get("permalink"):
        lines.append(f"Permalink: {slack_trigger_payload.get('permalink')}")
    lines.extend(["", f"Message text: {text}"])
    return "\n".join(lines)


def _clean(value: Any) -> str:
    return str(value or "").strip()


__all__ = [
    "SLACK_CHANNEL_MESSAGE_ORIGIN",
    "SLACK_MESSAGE_ENVELOPE_KIND",
    "SLACK_REPLY_TOOL",
    "SLACK_SURFACE",
    "build_slack_work_intake_payload",
    "slack_channel_monitor_message",
    "slack_event_type",
    "slack_response_target",
    "slack_run_message",
    "slack_surface",
    "slack_target",
    "slack_trigger",
]
