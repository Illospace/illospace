"""Slack run-request shaping for work intake."""

from __future__ import annotations

from typing import Any

from brain.systems.runs.domain import AgentRunRequest
from brain.systems.runs.work_intake import (
    _merge_trigger_metadata,
    _payload_user_id,
    _priority,
    _run_event,
    _trigger_dict,
    _trigger_org_id,
    _trigger_payload,
    _trigger_policy,
    _trigger_target,
    model_policy_from_metadata,
    profile_from_metadata,
    recipe_for_profile,
)

SLACK_SURFACE = "slack"
SLACK_REPLY_TOOL = "post_slack_reply"


def agent_run_request_for_slack(trigger_payload: dict[str, Any] | Any) -> AgentRunRequest:
    trigger = _trigger_dict(trigger_payload)
    target = _trigger_target(trigger)
    payload = _trigger_payload(trigger)
    policy = _trigger_policy(trigger)
    trigger_org_id = _trigger_org_id(trigger)
    metadata = _merge_trigger_metadata(
        trigger,
        payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None,
    )
    slack_trigger = dict(metadata.get("slack_trigger") or payload.get("slack") or {})
    surface_context = {
        "originating_surface": SLACK_SURFACE,
        "triggering_surface": SLACK_SURFACE,
        "source_surface": SLACK_SURFACE,
        "required_response_tool": SLACK_REPLY_TOOL,
        "final_answer_target_surface": SLACK_SURFACE,
    }
    for key, value in surface_context.items():
        metadata.setdefault(key, value)
    metadata["slack_trigger"] = slack_trigger

    run_event = _run_event(trigger, policy)
    priority = _priority(payload, policy)
    profile = profile_from_metadata(metadata)
    slack_thread_id = _slack_thread_id(slack_trigger, target)
    cortex_thread_id = str(target.get("idea_id") or target.get("thread_id") or "").strip()
    target_ref = {
        **target,
        "kind": "slack_message",
        "event": str(run_event),
        "surface": slack_trigger.get("surface") or target.get("surface") or SLACK_SURFACE,
        "slack_thread_id": slack_thread_id,
        "slack_trigger": slack_trigger,
        **surface_context,
    }
    if cortex_thread_id:
        target_ref["idea_id"] = cortex_thread_id
        target_ref["thread_id"] = cortex_thread_id
        target_ref["related_surfaces"] = {
            "slack": {
                "kind": "slack",
                "thread_id": slack_thread_id,
                "team_id": slack_trigger.get("team_id") or target.get("team_id"),
                "channel_id": slack_trigger.get("channel_id") or target.get("channel_id"),
                "message_ts": slack_trigger.get("message_ts") or target.get("message_ts"),
                "thread_ts": slack_trigger.get("thread_ts") or target.get("thread_ts"),
            }
        }
    return AgentRunRequest(
        org_id=trigger_org_id,
        user_id=_payload_user_id(payload, trigger, org_id=trigger_org_id),
        thread_id=cortex_thread_id or slack_thread_id,
        message=str(payload.get("run_message") or payload.get("message") or ""),
        profile=profile,
        recipe=recipe_for_profile(profile, metadata),
        target_ref=target_ref,
        workspace_ref={},
        model_policy=model_policy_from_metadata(metadata),
        metadata={
            **metadata,
            "event": str(run_event),
            "priority": priority,
            "source": f"trigger:{trigger.get('source')}",
            "producer": "trigger",
            "idempotency_key": trigger.get("idempotency_key"),
            "org_id": trigger_org_id,
        },
    )


def _slack_thread_id(slack_trigger: dict[str, Any], target: dict[str, Any]) -> str:
    team_id = str(slack_trigger.get("team_id") or target.get("team_id") or "")
    channel_id = str(slack_trigger.get("channel_id") or target.get("channel_id") or "")
    message_ts = str(
        slack_trigger.get("thread_ts")
        or slack_trigger.get("message_ts")
        or target.get("thread_ts")
        or target.get("message_ts")
        or ""
    )
    if not team_id or not channel_id or not message_ts:
        raise ValueError("Slack run triggers require team_id, channel_id, and message timestamp")
    return f"slack:{team_id}:{channel_id}:{message_ts}"


__all__ = ["agent_run_request_for_slack"]
