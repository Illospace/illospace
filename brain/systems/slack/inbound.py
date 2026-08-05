"""Slack-specific inbound envelope processing."""

from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from brain.kernel.common.coercion import as_mapping, optional_text
from brain.platform.db.models.external_agent import ExternalAgentConnectionRow
from brain.platform.db.models.inbound import InboundEventRow
from brain.platform.db.models.org import User
from brain.systems.inbound.handlers import (
    InboundCompletion,
    InboundEventCompleter,
    InboundHandlerContext,
)
from brain.systems.inbound.status import STATUS_PROCESSED
from brain.systems.inbound.surface_admission import (
    SurfaceAdmissionSpec,
    SurfaceIdentity,
    SurfaceTarget,
    admit_surface_envelope,
)
from brain.systems.personality.person_context import normalize_person_context
from brain.systems.slack.chantier_declare import (
    ChantierDeclareResult,
    apply_chantier_declare_run_contract,
    maybe_declare_chantier_from_slack,
)
from brain.systems.slack.identity import (
    SlackIdentityRecord,
    SlackIdentitySource,
    normalize_slack_identities,
)
from brain.systems.slack.monitored_intakes import monitored_intake_policy
from brain.systems.slack.triggers import (
    SLACK_MESSAGE_ENVELOPE_KIND,
    SLACK_REPLY_TOOL,
    build_slack_work_intake_payload,
)
from brain.systems.user_domains.service import DomainError, DomainNotFound

ACTION_SLACK_RUN_ADMITTED = "slack.run_admitted"


async def process_slack_message_envelope(
    session: AsyncSession,
    *,
    context: InboundHandlerContext,
    event: InboundEventRow,
    normalized: Mapping[str, Any],
    complete: InboundEventCompleter,
) -> dict[str, Any]:
    """Admit an Illo run for a normalized Slack mention or DM."""

    policy = monitored_intake_policy(normalized)
    if policy is not None and policy.interrupt is not None:
        payload = dict(normalized.get("payload") or {})
        response_target = dict(payload.get("response_target") or {})
        return await complete(
            InboundCompletion(
                status=STATUS_PROCESSED,
                action_type=policy.interrupt.action_type,
                action_result={
                    "operation": policy.interrupt.operation,
                    "event_id": str(event.id),
                    "origin": normalized.get("origin"),
                },
                confidence=1.0,
                target={
                    "kind": SLACK_MESSAGE_ENVELOPE_KIND,
                    "channel_id": response_target.get("channel_id"),
                    "thread_ts": response_target.get("thread_ts"),
                },
                tool_use={"type": SLACK_REPLY_TOOL, "status": "interrupt"},
                reasoning_summary=(
                    "A typed Slack intake policy routed this direct liveness "
                    "probe to a deterministic process-local reply."
                ),
                reusable_pattern_candidate={
                    "kind": SLACK_MESSAGE_ENVELOPE_KIND,
                    "origin": normalized.get("origin"),
                    "source_kind": context.source_kind,
                },
            )
        )

    return await admit_surface_envelope(
        session,
        context=context,
        event=event,
        normalized=normalized,
        complete=complete,
        spec=SLACK_ADMISSION,
    )


async def _resolve_slack_identity(
    session: AsyncSession,
    context: InboundHandlerContext,
    normalized: Mapping[str, Any],
) -> SurfaceIdentity:
    authority_user_id, person_context = await _slack_run_identity(
        session,
        context=context,
        normalized=normalized,
    )
    return SurfaceIdentity(
        authority_user_id=authority_user_id,
        details={"person_context": person_context},
    )


async def _build_slack_payload(
    session: AsyncSession,
    context: InboundHandlerContext,
    event: InboundEventRow,
    normalized: Mapping[str, Any],
    identity: SurfaceIdentity,
) -> dict[str, Any]:
    slack_payload = dict(normalized.get("payload") or {})
    chantier_declare: ChantierDeclareResult | None = None
    chantier_declare_error: str | None = None
    try:
        chantier_declare = await maybe_declare_chantier_from_slack(
            session,
            org_id=str(context.org_id),
            actor_user_id=str(identity.authority_user_id),
            origin=str(normalized.get("origin") or ""),
            text=str(slack_payload.get("text") or ""),
            channel_id=str(slack_payload.get("channel_id") or "") or None,
            thread_ts=str(slack_payload.get("thread_ts") or "") or None,
        )
    except (DomainError, DomainNotFound) as exc:
        # Missing/mismatched Domain-1 configuration must be visible in the
        # declaration thread, but it must not take down the normal Slack lane.
        chantier_declare_error = str(exc)

    slack_thread_id = _slack_conversation_thread_id(slack_payload)
    trigger_payload = build_slack_work_intake_payload(
        org_id=context.org_id,
        authority_user_id=str(identity.authority_user_id),
        payload=slack_payload,
        inbound_event_id=str(event.id),
        connection_id=context.connection_id,
        idempotency_key=optional_text(normalized.get("idempotency_key")),
        person_context=identity.details.get("person_context"),
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
    return trigger_payload


def _slack_target(
    _trigger_payload: Mapping[str, Any],
    normalized: Mapping[str, Any],
) -> SurfaceTarget:
    slack_payload = dict(normalized.get("payload") or {})
    slack_thread_id = _slack_conversation_thread_id(slack_payload)
    target = {
        "kind": "slack_message",
        "team_id": slack_payload.get("team_id"),
        "channel_id": slack_payload.get("channel_id"),
        "message_ts": slack_payload.get("message_ts"),
        "thread_ts": slack_payload.get("thread_ts"),
        "slack_thread_id": slack_thread_id,
    }
    return SurfaceTarget(
        value=target,
        tool_context={"slack_thread_id": slack_thread_id},
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
    context: InboundHandlerContext,
    normalized: Mapping[str, Any],
) -> str | None:
    """Resolve Slack actor mapping, falling back to connection authority."""

    authority_user_id, _person_context = await _slack_run_identity(
        session,
        context=context,
        normalized=normalized,
    )
    return authority_user_id


async def _slack_run_identity(
    session: AsyncSession,
    *,
    context: InboundHandlerContext,
    normalized: Mapping[str, Any],
) -> tuple[str | None, dict[str, Any] | None]:
    """Resolve execution authority and a separate verified speaker identity."""

    payload = dict(normalized.get("payload") or {})
    slack_user_id = optional_text(payload.get("slack_user_id"))
    if slack_user_id:
        connection = await session.get(ExternalAgentConnectionRow, context.connection_id)
        if connection is not None:
            records, _conflicts = normalize_slack_identities(
                connection.metadata_ or {}
            )
            record = records.get(slack_user_id)
            mapped_user_id = record.user_id if record is not None else None
            if mapped_user_id:
                user = await session.get(User, mapped_user_id)
                if user is not None and str(user.org_id) == str(context.org_id):
                    person_context = _linked_slack_person_context(
                        record,
                        channel_type=payload.get("channel_type"),
                    )
                    return mapped_user_id, person_context
    return context.owner_user_id, None


def _linked_slack_person_context(
    record: SlackIdentityRecord,
    *,
    channel_type: Any,
) -> dict[str, Any] | None:
    """Use normalized Slack preferences only after authority reconciliation."""

    if (
        SlackIdentitySource.LINK not in record.sources
        or record.user_id is None
        or record.linked_user_id != record.user_id
    ):
        return None

    preferences = as_mapping(
        record.link_metadata.get("communication_preferences")
    )
    if (optional_text(channel_type) or "").lower() == "im":
        if record.link_display_name:
            preferences.setdefault("address_as", record.link_display_name)
    else:
        preferences.pop("address_as", None)

    person_context = normalize_person_context(
        {
            "mapping": "verified",
            "user_id": record.user_id,
            "source": "slack_identity_link",
            "preferences": preferences,
        },
        verified_user_id=record.user_id,
    )
    return person_context or None


SLACK_ADMISSION = SurfaceAdmissionSpec(
    kind=SLACK_MESSAGE_ENVELOPE_KIND,
    action_type=ACTION_SLACK_RUN_ADMITTED,
    success_operation="slack_run_admitted",
    failure_operation="slack_run_admission_failed",
    tool_type="slack_teammate_run",
    resolve_identity=_resolve_slack_identity,
    build_payload=_build_slack_payload,
    build_target=_slack_target,
    outcome_target_key="slack",
    success_reasoning=(
        "Slack mention or DM was admitted as a surface-aware Illo run. Illo decides "
        "whether to answer directly or create durable Cortex/worker follow-up."
    ),
    admission_failure_reasoning="Slack event could not be admitted as an Illo run.",
    missing_authority_error="Slack connection has no authority user",
    missing_authority_reasoning=(
        "Slack events need a connection owner for the permissive self-hosted MVP."
    ),
)


__all__ = ["process_slack_message_envelope"]
