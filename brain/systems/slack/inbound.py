"""Slack-specific inbound envelope processing."""

from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from brain.app.triggers.adapters.slack import build_slack_message_trigger
from brain.platform.db.models.external_agent import ExternalAgentConnectionRow
from brain.platform.db.models.inbound import InboundEventRow
from brain.platform.db.models.org import User
from brain.systems.inbound.service import _clean_optional, _complete_event
from brain.systems.inbound.status import STATUS_FAILED, STATUS_PROCESSED
from brain.systems.runs.work_intake import WorkIntakeEvent, admit_work

ACTION_SLACK_RUN_ADMITTED = "slack.run_admitted"
SLACK_MESSAGE_ENVELOPE_KIND = "slack_message"


async def process_slack_message_envelope(
    session: AsyncSession,
    *,
    context: Any,
    event: InboundEventRow,
    normalized: Mapping[str, Any],
) -> dict[str, Any]:
    """Admit an Illo run for a normalized Slack mention or DM."""

    authority_user_id = await _slack_run_user_id(session, context=context, normalized=normalized)
    if not authority_user_id:
        return await _complete_event(
            session,
            event,
            policy=None,
            status=STATUS_FAILED,
            action_type=ACTION_SLACK_RUN_ADMITTED,
            action_result={
                "operation": "slack_run_admission_failed",
                "reason": "missing_authority_user",
                "event_id": str(event.id),
            },
            confidence=0.0,
            error="Slack connection has no authority user",
            target={"kind": "slack_message"},
            tool_use={"type": "slack_teammate_run", "status": "failed"},
            reasoning_summary="Slack events need a connection owner for the permissive self-hosted MVP.",
        )

    trigger = build_slack_message_trigger(
        org_id=context.org_id,
        authority_user_id=authority_user_id,
        payload=dict(normalized.get("payload") or {}),
        inbound_event_id=str(event.id),
        connection_id=context.connection_id,
        idempotency_key=_clean_optional(normalized.get("idempotency_key")),
    )
    admission = await admit_work(
        session,
        WorkIntakeEvent.from_trigger_payload(trigger.to_payload()),
    )
    slack_payload = dict(normalized.get("payload") or {})
    target = {
        "kind": "slack_message",
        "team_id": slack_payload.get("team_id"),
        "channel_id": slack_payload.get("channel_id"),
        "message_ts": slack_payload.get("message_ts"),
        "thread_ts": slack_payload.get("thread_ts"),
    }
    if admission.ok:
        if admission.run_id is not None:
            target["run_id"] = admission.run_id
        return await _complete_event(
            session,
            event,
            policy=None,
            status=STATUS_PROCESSED,
            action_type=ACTION_SLACK_RUN_ADMITTED,
            action_result={
                "operation": "slack_run_admitted",
                "run_id": admission.run_id,
                "event_id": str(event.id),
                "origin": normalized.get("origin"),
                "slack": target,
            },
            confidence=1.0,
            target=target,
            tool_use={"type": "slack_teammate_run", "run_id": admission.run_id},
            reasoning_summary="Slack mention or DM was admitted as a surface-aware Illo run.",
            reusable_pattern_candidate={
                "kind": SLACK_MESSAGE_ENVELOPE_KIND,
                "origin": normalized.get("origin"),
                "source_kind": context.source_kind,
            },
        )

    return await _complete_event(
        session,
        event,
        policy=None,
        status=STATUS_FAILED,
        action_type=ACTION_SLACK_RUN_ADMITTED,
        action_result={
            "operation": "slack_run_admission_failed",
            "reason": admission.skipped_reason or "run_admission_failed",
            "event_id": str(event.id),
            "origin": normalized.get("origin"),
            "slack": target,
        },
        confidence=0.0,
        error=admission.skipped_reason or "run_admission_failed",
        target=target,
        tool_use={"type": "slack_teammate_run", "status": "failed"},
        reasoning_summary=admission.skipped_reason or "Slack event could not be admitted as an Illo run.",
    )


async def _slack_run_user_id(
    session: AsyncSession,
    *,
    context: Any,
    normalized: Mapping[str, Any],
) -> str | None:
    """Resolve Slack actor mapping, falling back to connection authority."""

    payload = dict(normalized.get("payload") or {})
    slack_user_id = _clean_optional(payload.get("slack_user_id"))
    if slack_user_id:
        connection = await session.get(ExternalAgentConnectionRow, context.connection_id)
        if connection is not None:
            metadata = dict(connection.metadata_ or {})
            slack_metadata = metadata.get("slack")
            if isinstance(slack_metadata, Mapping):
                identity_map = slack_metadata.get("identity_map")
                if isinstance(identity_map, Mapping):
                    mapped_user_id = _clean_optional(identity_map.get(slack_user_id))
                    if mapped_user_id:
                        user = await session.get(User, mapped_user_id)
                        if user is not None and str(user.org_id) == str(context.org_id):
                            return mapped_user_id
    return context.owner_user_id


__all__ = ["process_slack_message_envelope"]
