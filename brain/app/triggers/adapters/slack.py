"""Slack trigger adapter for teammate-style Illo mentions and DMs."""

from __future__ import annotations

from typing import Any, Mapping

from brain.app.api.authorization import PrincipalIdentity
from brain.app.triggers.contracts import IlloTrigger, stable_idempotency_key
from brain.systems.slack.triggers import build_slack_work_intake_payload


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

    trigger_payload = build_slack_work_intake_payload(
        org_id=org_id,
        authority_user_id=authority_user_id,
        payload=payload,
        inbound_event_id=inbound_event_id,
        connection_id=connection_id,
        idempotency_key=idempotency_key,
        priority=priority,
    )
    key = trigger_payload.get("idempotency_key") or stable_idempotency_key(
        source="slack",
        event_type=str(trigger_payload["event_type"]),
        org_id=str(org_id),
        target=dict(trigger_payload["target"]),
        payload={
            "message_ts": dict(trigger_payload["target"]).get("message_ts"),
            "priority": int(priority),
        },
    )
    actor_payload = dict(trigger_payload["actor"])
    actor = PrincipalIdentity(
        id=str(actor_payload["id"]),
        principal_type=str(actor_payload["principal_type"]),
        role=str(actor_payload["role"]),
        name=str(actor_payload["name"]),
        org_id=str(actor_payload.get("org_id") or org_id),
        metadata=dict(actor_payload.get("metadata") or {}),
    )
    return IlloTrigger(
        source=str(trigger_payload["source"]),
        event_type=str(trigger_payload["event_type"]),
        actor=actor,
        org_id=str(trigger_payload["org_id"]),
        target=dict(trigger_payload["target"]),
        payload=dict(trigger_payload["payload"]),
        idempotency_key=str(key),
        policy=dict(trigger_payload["policy"]),
    )


__all__ = ["build_slack_message_trigger"]
