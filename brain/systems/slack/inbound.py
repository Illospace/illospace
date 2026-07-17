"""Slack-specific inbound envelope processing."""

from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.external_agent import ExternalAgentConnectionRow
from brain.platform.db.models.inbound import InboundEventRow
from brain.platform.db.models.org import User
from brain.systems.inbound.service import _clean_optional, _complete_event
from brain.systems.inbound.status import STATUS_FAILED, STATUS_PROCESSED
from brain.systems.runs.work_intake import WorkIntakeEvent, admit_work
from brain.systems.slack.chantier_declare import (
    ChantierDeclareResult,
    apply_chantier_declare_run_contract,
    maybe_declare_chantier_from_slack,
)
from brain.systems.slack.triggers import (
    SLACK_MESSAGE_ENVELOPE_KIND,
    build_slack_work_intake_payload,
)
from brain.systems.user_domains.service import DomainError, DomainNotFound

ACTION_SLACK_RUN_ADMITTED = "slack.run_admitted"


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

    slack_payload = dict(normalized.get("payload") or {})
    chantier_declare: ChantierDeclareResult | None = None
    chantier_declare_error: str | None = None
    try:
        chantier_declare = await maybe_declare_chantier_from_slack(
            session,
            org_id=str(context.org_id),
            actor_user_id=authority_user_id,
            origin=str(normalized.get("origin") or ""),
            text=str(slack_payload.get("text") or ""),
        )
    except (DomainError, DomainNotFound) as exc:
        # Missing/mismatched Domain-1 configuration must be visible in the
        # declaration thread, but it must not take down the normal Slack lane.
        chantier_declare_error = str(exc)

    slack_thread_id = _slack_conversation_thread_id(slack_payload)
    trigger_payload = build_slack_work_intake_payload(
        org_id=context.org_id,
        authority_user_id=authority_user_id,
        payload=slack_payload,
        inbound_event_id=str(event.id),
        connection_id=context.connection_id,
        idempotency_key=_clean_optional(normalized.get("idempotency_key")),
    )
    if chantier_declare is not None or chantier_declare_error is not None:
        apply_chantier_declare_run_contract(
            trigger_payload,
            result=chantier_declare,
            error=chantier_declare_error,
        )
    trigger_metadata = dict((trigger_payload.get("payload") or {}).get("metadata") or {})
    trigger_metadata["slack_thread_id"] = slack_thread_id
    trigger_payload["payload"] = {
        **dict(trigger_payload.get("payload") or {}),
        "metadata": trigger_metadata,
    }
    admission = await admit_work(
        session,
        WorkIntakeEvent.from_trigger_payload(trigger_payload),
    )
    target = {
        "kind": "slack_message",
        "team_id": slack_payload.get("team_id"),
        "channel_id": slack_payload.get("channel_id"),
        "message_ts": slack_payload.get("message_ts"),
        "thread_ts": slack_payload.get("thread_ts"),
        "slack_thread_id": slack_thread_id,
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
            tool_use={
                "type": "slack_teammate_run",
                "run_id": admission.run_id,
                "slack_thread_id": slack_thread_id,
            },
            reasoning_summary=(
                "Slack mention or DM was admitted as a surface-aware Illo run. Illo decides "
                "whether to answer directly or create durable Cortex/worker follow-up."
            ),
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


def _slack_conversation_thread_id(payload: Mapping[str, Any]) -> str:
    team_id = str(payload.get("team_id") or "").strip()
    channel_id = str(payload.get("channel_id") or "").strip()
    thread_ts = str(payload.get("thread_ts") or payload.get("message_ts") or "").strip()
    if not team_id or not channel_id or not thread_ts:
        return ""
    return f"slack:{team_id}:{channel_id}:{thread_ts}"


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
