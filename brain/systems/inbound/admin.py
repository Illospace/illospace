"""Illo-facing administration helpers for inbound coordination."""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Any, Mapping, Sequence

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.external_agent import (
    ExternalAgentConnectionRow,
    ExternalAgentConnectionTokenRow,
)
from brain.platform.db.models.inbound import (
    InboundDecisionReceiptRow,
    InboundDomainProjectionRow,
    InboundEventRow,
    InboundSourcePolicyRow,
)
from brain.systems.external_agents import service as external_agents
from brain.systems.inbound import service as inbound_service
from brain.systems.runs.failures import public_run_failure


DEFAULT_SIGNAL_TOKEN_SCOPES = (external_agents.SCOPE_SIGNAL_SUBMIT,)
READ_LIMIT_MAX = 100
ATTENTION_EVENT_STATUSES = (
    inbound_service.STATUS_FAILED,
    inbound_service.STATUS_QUARANTINED,
    inbound_service.STATUS_REVIEW_REQUIRED,
)


class InboundAdminError(RuntimeError):
    """Raised when an Illo-facing inbound admin operation is invalid."""


def _iso(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else (str(value) if value else None)


def _json_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple | set | frozenset):
        return list(value)
    return []


def _limit(value: int | None) -> int:
    try:
        parsed = int(value or 25)
    except Exception:
        parsed = 25
    return max(1, min(parsed, READ_LIMIT_MAX))


def _set_if_not_none(row: Any, field: str, value: Any) -> None:
    if value is not None:
        setattr(row, field, value)


def _stripped_strings(values: Sequence[Any]) -> list[str]:
    return [str(item).strip() for item in values if str(item).strip()]


async def require_connection_for_org(
    session: AsyncSession,
    *,
    org_id: str,
    connection_id: str,
) -> ExternalAgentConnectionRow:
    row = await session.get(ExternalAgentConnectionRow, str(connection_id))
    if row is None or str(row.org_id) != str(org_id):
        raise InboundAdminError("External source connection not found")
    return row


async def create_connection(
    session: AsyncSession,
    *,
    org_id: str,
    owner_user_id: str,
    display_name: str,
    agent_kind: str = "custom",
    transport: str = "webhook",
    endpoint_url: str | None = None,
    remote_agent_id: str | None = None,
    remote_agent_card: Mapping[str, Any] | None = None,
    capabilities: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> ExternalAgentConnectionRow:
    """Create an external source connection owned by the current user."""

    row = await external_agents.create_connection(
        session,
        org_id=str(org_id),
        owner_user_id=str(owner_user_id),
        display_name=display_name,
        agent_kind=agent_kind,
        transport=transport,
        endpoint_url=endpoint_url,
        remote_agent_id=remote_agent_id,
        remote_agent_card=remote_agent_card,
        capabilities=capabilities or {"submit_signals": True},
        metadata=metadata,
    )
    row.status = "configured"
    await session.flush()
    await session.refresh(row)
    return row


async def update_connection(
    session: AsyncSession,
    *,
    org_id: str,
    connection_id: str,
    display_name: str | None = None,
    status: str | None = None,
    endpoint_url: str | None = None,
    remote_agent_id: str | None = None,
    remote_agent_card: Mapping[str, Any] | None = None,
    capabilities: Mapping[str, Any] | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> ExternalAgentConnectionRow:
    row = await require_connection_for_org(session, org_id=org_id, connection_id=connection_id)
    _set_if_not_none(row, "display_name", display_name)
    _set_if_not_none(row, "status", status)
    if status is not None:
        normalized_status = str(status or "").strip().lower()
        row.disabled_at = inbound_service.utcnow() if normalized_status == "disabled" else None
    _set_if_not_none(row, "endpoint_url", endpoint_url)
    _set_if_not_none(row, "remote_agent_id", remote_agent_id)
    if remote_agent_card is not None:
        row.remote_agent_card = dict(remote_agent_card)
    if capabilities is not None:
        row.capabilities = dict(capabilities)
    if metadata is not None:
        row.metadata_ = dict(metadata)
    await session.flush()
    await session.refresh(row)
    return row


async def list_connections(
    session: AsyncSession,
    *,
    org_id: str,
    agent_kind: str | None = None,
    transport: str | None = None,
    include_disabled: bool = False,
    limit: int | None = 25,
) -> list[ExternalAgentConnectionRow]:
    stmt = (
        select(ExternalAgentConnectionRow)
        .where(ExternalAgentConnectionRow.org_id == str(org_id))
        .order_by(ExternalAgentConnectionRow.created_at.desc(), ExternalAgentConnectionRow.id.desc())
        .limit(_limit(limit))
    )
    if agent_kind:
        stmt = stmt.where(ExternalAgentConnectionRow.agent_kind == str(agent_kind).strip().lower())
    if transport:
        stmt = stmt.where(ExternalAgentConnectionRow.transport == str(transport).strip().lower())
    if not include_disabled:
        stmt = stmt.where(
            ExternalAgentConnectionRow.disabled_at.is_(None),
            func.lower(ExternalAgentConnectionRow.status) != "disabled",
        )
    return list((await session.scalars(stmt)).all())


async def mint_signal_token(
    session: AsyncSession,
    *,
    org_id: str,
    connection_id: str,
    name: str = "Inbound signal token",
    scopes: Sequence[str] | None = None,
    expires_at: datetime | None = None,
) -> tuple[str, ExternalAgentConnectionTokenRow]:
    """Mint a least-privilege inbound token unless explicit scopes are supplied."""

    await require_connection_for_org(session, org_id=org_id, connection_id=connection_id)
    return await external_agents.mint_connection_token(
        session,
        connection_id=connection_id,
        org_id=org_id,
        name=name,
        scopes=list(scopes or DEFAULT_SIGNAL_TOKEN_SCOPES),
        expires_at=expires_at,
    )


async def list_tokens(
    session: AsyncSession,
    *,
    org_id: str,
    connection_id: str | None = None,
    include_revoked: bool = False,
    limit: int | None = 25,
) -> list[ExternalAgentConnectionTokenRow]:
    stmt = (
        select(ExternalAgentConnectionTokenRow)
        .where(ExternalAgentConnectionTokenRow.org_id == str(org_id))
        .order_by(ExternalAgentConnectionTokenRow.created_at.desc(), ExternalAgentConnectionTokenRow.id.desc())
        .limit(_limit(limit))
    )
    if connection_id:
        await require_connection_for_org(session, org_id=org_id, connection_id=connection_id)
        stmt = stmt.where(ExternalAgentConnectionTokenRow.connection_id == str(connection_id))
    if not include_revoked:
        stmt = stmt.where(ExternalAgentConnectionTokenRow.revoked_at.is_(None))
    return list((await session.scalars(stmt)).all())


async def require_token_for_org(
    session: AsyncSession,
    *,
    org_id: str,
    token_id: str,
) -> ExternalAgentConnectionTokenRow:
    row = await session.get(ExternalAgentConnectionTokenRow, str(token_id))
    if row is None or str(row.org_id) != str(org_id):
        raise InboundAdminError("Connection token not found")
    return row


async def revoke_token(
    session: AsyncSession,
    *,
    org_id: str,
    token_id: str,
) -> ExternalAgentConnectionTokenRow:
    row = await require_token_for_org(session, org_id=org_id, token_id=token_id)
    row.revoked_at = inbound_service.utcnow()
    await session.flush()
    return row


async def list_policies(
    session: AsyncSession,
    *,
    org_id: str,
    connection_id: str | None = None,
    include_disabled: bool = False,
    limit: int | None = 25,
) -> list[InboundSourcePolicyRow]:
    stmt = (
        select(InboundSourcePolicyRow)
        .where(InboundSourcePolicyRow.org_id == str(org_id))
        .order_by(InboundSourcePolicyRow.priority.asc(), InboundSourcePolicyRow.created_at.desc())
        .limit(_limit(limit))
    )
    if connection_id:
        stmt = stmt.where(InboundSourcePolicyRow.connection_id == str(connection_id))
    if not include_disabled:
        stmt = stmt.where(InboundSourcePolicyRow.enabled.is_(True))
    return list((await session.scalars(stmt)).all())


async def require_policy_for_org(
    session: AsyncSession,
    *,
    org_id: str,
    policy_id: str,
) -> InboundSourcePolicyRow:
    row = await session.get(InboundSourcePolicyRow, str(policy_id))
    if row is None or str(row.org_id) != str(org_id):
        raise InboundAdminError("Inbound source policy not found")
    return row


async def create_policy(
    session: AsyncSession,
    *,
    org_id: str,
    connection_id: str,
    name: str,
    origin_patterns: Sequence[str],
    priority: int = 100,
    envelope_kinds: Sequence[str] | None = None,
    instructions: str | None = None,
    schema_config: Mapping[str, Any] | None = None,
    allowed_actions: Sequence[str] | None = None,
    auto_execute_actions: Sequence[str] | None = None,
    auto_execute_min_confidence: float = 0.85,
    review_mode: str = inbound_service.STATUS_REVIEW_REQUIRED,
    metadata: Mapping[str, Any] | None = None,
    enabled: bool = True,
) -> InboundSourcePolicyRow:
    await require_connection_for_org(session, org_id=org_id, connection_id=connection_id)
    row = await inbound_service.create_source_policy(
        session,
        org_id=org_id,
        connection_id=connection_id,
        name=name,
        origin_patterns=origin_patterns,
        priority=priority,
        envelope_kinds=envelope_kinds,
        instructions=instructions,
        schema_config=schema_config,
        allowed_actions=allowed_actions,
        auto_execute_actions=auto_execute_actions,
        auto_execute_min_confidence=auto_execute_min_confidence,
        review_mode=review_mode,
        metadata=metadata,
        enabled=enabled,
    )
    await session.refresh(row)
    return row


async def update_policy(
    session: AsyncSession,
    *,
    org_id: str,
    policy_id: str,
    name: str | None = None,
    enabled: bool | None = None,
    priority: int | None = None,
    origin_patterns: Sequence[str] | None = None,
    envelope_kinds: Sequence[str] | None = None,
    instructions: str | None = None,
    schema_config: Mapping[str, Any] | None = None,
    allowed_actions: Sequence[str] | None = None,
    auto_execute_actions: Sequence[str] | None = None,
    auto_execute_min_confidence: float | None = None,
    review_mode: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> InboundSourcePolicyRow:
    row = await require_policy_for_org(session, org_id=org_id, policy_id=policy_id)
    _set_if_not_none(row, "name", name)
    _set_if_not_none(row, "enabled", enabled)
    _set_if_not_none(row, "priority", int(priority) if priority is not None else None)
    _set_if_not_none(row, "instructions", instructions)
    _set_if_not_none(
        row,
        "auto_execute_min_confidence",
        float(auto_execute_min_confidence) if auto_execute_min_confidence is not None else None,
    )
    _set_if_not_none(row, "review_mode", review_mode)
    if origin_patterns is not None:
        row.origin_patterns = _stripped_strings(origin_patterns)
    if envelope_kinds is not None:
        row.envelope_kinds = _stripped_strings(envelope_kinds)
    if schema_config is not None:
        row.schema_config = dict(schema_config)
    if allowed_actions is not None:
        row.allowed_actions = _stripped_strings(allowed_actions)
    if auto_execute_actions is not None:
        row.auto_execute_actions = _stripped_strings(auto_execute_actions)
    if metadata is not None:
        row.metadata_ = dict(metadata)
    await session.flush()
    await session.refresh(row)
    return row


async def list_projections(
    session: AsyncSession,
    *,
    org_id: str,
    connection_id: str | None = None,
    policy_id: str | None = None,
    include_disabled: bool = False,
    limit: int | None = 25,
) -> list[InboundDomainProjectionRow]:
    stmt = (
        select(InboundDomainProjectionRow)
        .where(InboundDomainProjectionRow.org_id == str(org_id))
        .order_by(InboundDomainProjectionRow.created_at.desc(), InboundDomainProjectionRow.id.desc())
        .limit(_limit(limit))
    )
    if connection_id:
        stmt = stmt.where(InboundDomainProjectionRow.connection_id == str(connection_id))
    if policy_id:
        stmt = stmt.where(InboundDomainProjectionRow.policy_id == str(policy_id))
    if not include_disabled:
        stmt = stmt.where(InboundDomainProjectionRow.enabled.is_(True))
    return list((await session.scalars(stmt)).all())


async def require_projection_for_org(
    session: AsyncSession,
    *,
    org_id: str,
    projection_id: str,
) -> InboundDomainProjectionRow:
    row = await session.get(InboundDomainProjectionRow, str(projection_id))
    if row is None or str(row.org_id) != str(org_id):
        raise InboundAdminError("Inbound Domain Projection not found")
    return row


async def create_projection(
    session: AsyncSession,
    *,
    org_id: str,
    connection_id: str,
    domain_id: int,
    object_key: str,
    external_id_path: str,
    external_id_field: str,
    field_mapping: Mapping[str, str],
    policy_id: str | None = None,
    title_path: str | None = None,
    upsert_mode: str = "upsert",
    validation_failure_status: str = inbound_service.STATUS_REVIEW_REQUIRED,
    metadata: Mapping[str, Any] | None = None,
    enabled: bool = True,
    auto_allow_policy_action: bool = True,
) -> InboundDomainProjectionRow:
    await require_connection_for_org(session, org_id=org_id, connection_id=connection_id)
    policy = None
    if policy_id:
        policy = await require_policy_for_org(session, org_id=org_id, policy_id=policy_id)
        if str(policy.connection_id) != str(connection_id):
            raise InboundAdminError("Projection policy must belong to the same connection")

    projection = await inbound_service.create_domain_projection(
        session,
        org_id=org_id,
        connection_id=connection_id,
        domain_id=domain_id,
        object_key=object_key,
        external_id_path=external_id_path,
        external_id_field=external_id_field,
        field_mapping=field_mapping,
        policy_id=policy_id,
        title_path=title_path,
        upsert_mode=upsert_mode,
        validation_failure_status=validation_failure_status,
        metadata=metadata,
        enabled=enabled,
    )
    if policy is not None and auto_allow_policy_action:
        actions = {str(item).strip() for item in _json_list(policy.allowed_actions) if str(item).strip()}
        actions.add(inbound_service.ACTION_DOMAIN_PROJECTION_UPSERT)
        policy.allowed_actions = sorted(actions)
        await session.flush()
        await session.refresh(policy)
    await session.refresh(projection)
    return projection


async def update_projection(
    session: AsyncSession,
    *,
    org_id: str,
    projection_id: str,
    policy_id: str | None = None,
    domain_id: int | None = None,
    object_key: str | None = None,
    enabled: bool | None = None,
    external_id_path: str | None = None,
    external_id_field: str | None = None,
    field_mapping: Mapping[str, str] | None = None,
    title_path: str | None = None,
    upsert_mode: str | None = None,
    validation_failure_status: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> InboundDomainProjectionRow:
    row = await require_projection_for_org(session, org_id=org_id, projection_id=projection_id)
    if policy_id is not None:
        policy = await require_policy_for_org(session, org_id=org_id, policy_id=policy_id)
        if str(policy.connection_id) != str(row.connection_id):
            raise InboundAdminError("Projection policy must belong to the same connection")
        row.policy_id = str(policy_id) if policy_id else None
    _set_if_not_none(row, "domain_id", int(domain_id) if domain_id is not None else None)
    _set_if_not_none(row, "object_key", object_key)
    _set_if_not_none(row, "enabled", enabled)
    _set_if_not_none(row, "external_id_path", external_id_path)
    _set_if_not_none(row, "external_id_field", external_id_field)
    _set_if_not_none(row, "title_path", title_path)
    _set_if_not_none(row, "upsert_mode", upsert_mode)
    _set_if_not_none(row, "validation_failure_status", validation_failure_status)
    if field_mapping is not None:
        row.field_mapping = {str(key): str(value) for key, value in dict(field_mapping).items()}
    if metadata is not None:
        row.metadata_ = dict(metadata)
    await session.flush()
    await session.refresh(row)
    return row


async def list_events(
    session: AsyncSession,
    *,
    org_id: str,
    connection_id: str | None = None,
    policy_id: str | None = None,
    status: str | None = None,
    origin: str | None = None,
    limit: int | None = 25,
) -> list[InboundEventRow]:
    stmt = (
        select(InboundEventRow)
        .where(InboundEventRow.org_id == str(org_id))
        .order_by(InboundEventRow.created_at.desc(), InboundEventRow.id.desc())
        .limit(_limit(limit))
    )
    if connection_id:
        stmt = stmt.where(InboundEventRow.connection_id == str(connection_id))
    if policy_id:
        stmt = stmt.where(InboundEventRow.policy_id == str(policy_id))
    if status:
        stmt = stmt.where(InboundEventRow.status == str(status))
    if origin:
        stmt = stmt.where(InboundEventRow.origin == str(origin))
    return list((await session.scalars(stmt)).all())


async def list_attention_events(
    session: AsyncSession,
    *,
    org_id: str,
    connection_id: str | None = None,
    policy_id: str | None = None,
    origin: str | None = None,
    limit: int | None = 25,
    include_payload: bool = False,
) -> dict[str, Any]:
    """List inbound events that need Illo/operator attention."""

    stmt = (
        select(InboundEventRow)
        .where(InboundEventRow.org_id == str(org_id))
        .where(
            or_(
                InboundEventRow.status.in_(ATTENTION_EVENT_STATUSES),
                InboundEventRow.error.is_not(None),
            )
        )
        .order_by(InboundEventRow.created_at.desc(), InboundEventRow.id.desc())
        .limit(_limit(limit))
    )
    if connection_id:
        stmt = stmt.where(InboundEventRow.connection_id == str(connection_id))
    if policy_id:
        stmt = stmt.where(InboundEventRow.policy_id == str(policy_id))
    if origin:
        stmt = stmt.where(InboundEventRow.origin == str(origin))
    rows = list((await session.scalars(stmt)).all())
    return {
        "events": [serialize_event(row, include_payload=include_payload) for row in rows],
        "summary": _attention_summary(rows),
    }


async def require_event_for_org(
    session: AsyncSession,
    *,
    org_id: str,
    event_id: str,
) -> InboundEventRow:
    row = await session.get(InboundEventRow, str(event_id))
    if row is None or str(row.org_id) != str(org_id):
        raise InboundAdminError("Inbound event not found")
    return row


async def list_receipts(
    session: AsyncSession,
    *,
    org_id: str,
    event_id: str | None = None,
    limit: int | None = 25,
) -> list[InboundDecisionReceiptRow]:
    stmt = (
        select(InboundDecisionReceiptRow)
        .where(InboundDecisionReceiptRow.org_id == str(org_id))
        .order_by(InboundDecisionReceiptRow.created_at.desc(), InboundDecisionReceiptRow.id.desc())
        .limit(_limit(limit))
    )
    if event_id:
        stmt = stmt.where(InboundDecisionReceiptRow.event_id == str(event_id))
    return list((await session.scalars(stmt)).all())


async def dry_run_match(
    session: AsyncSession,
    *,
    org_id: str,
    connection_id: str,
    origin: str,
    kind: str = "signal",
    payload: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Preview source policy/projection matching without storing an event."""

    await require_connection_for_org(session, org_id=org_id, connection_id=connection_id)
    return await _preview_envelope(
        session,
        org_id=org_id,
        connection_id=connection_id,
        kind=kind,
        origin=origin,
        payload=payload,
    )


async def replay_events(
    session: AsyncSession,
    *,
    org_id: str,
    event_id: str | None = None,
    connection_id: str | None = None,
    policy_id: str | None = None,
    status: str | None = None,
    origin: str | None = None,
    limit: int | None = 25,
    include_payload: bool = False,
) -> dict[str, Any]:
    """Replay stored events against current matching config without mutating state."""

    if event_id:
        rows = [await require_event_for_org(session, org_id=org_id, event_id=event_id)]
    else:
        rows = await list_events(
            session,
            org_id=org_id,
            connection_id=connection_id,
            policy_id=policy_id,
            status=status,
            origin=origin,
            limit=limit,
        )

    results = [await _replay_event(session, org_id=org_id, row=row, include_payload=include_payload) for row in rows]
    return {
        "mode": "dry_run_replay",
        "mutates_workspace": False,
        "event_count": len(results),
        "summary": _summarize_replay(results),
        "results": results,
    }


async def get_source_card(
    session: AsyncSession,
    *,
    org_id: str,
    connection_id: str,
    limit: int | None = 50,
) -> dict[str, Any]:
    """Return the current computed source card plus the last persisted card."""

    connection = await require_connection_for_org(session, org_id=org_id, connection_id=connection_id)
    metadata = _json_dict(connection.metadata_)
    return {
        "source_card": await _build_source_card(
            session,
            org_id=org_id,
            connection=connection,
            limit=limit,
            manual=_json_dict(metadata.get("source_card_manual")),
        ),
        "persisted_source_card": _json_dict(metadata.get("source_card")) or None,
    }


async def refresh_source_card(
    session: AsyncSession,
    *,
    org_id: str,
    connection_id: str,
    purpose: str | None = None,
    notes: str | None = None,
    tags: Sequence[str] | None = None,
    limit: int | None = 50,
) -> dict[str, Any]:
    """Persist a refreshed source card summary on the connection metadata."""

    connection = await require_connection_for_org(session, org_id=org_id, connection_id=connection_id)
    metadata = _json_dict(connection.metadata_)
    manual = _source_card_manual(metadata, purpose=purpose, notes=notes, tags=tags)
    card = await _build_source_card(
        session,
        org_id=org_id,
        connection=connection,
        limit=limit,
        manual=manual,
        refreshed=True,
    )
    connection.metadata_ = {
        **metadata,
        "source_card_manual": manual,
        "source_card": card,
    }
    await session.flush()
    await session.refresh(connection)
    return {"source_card": card}


async def _preview_envelope(
    session: AsyncSession,
    *,
    org_id: str,
    connection_id: str,
    origin: str,
    kind: str = "signal",
    payload: Mapping[str, Any] | None = None,
    summary: str | None = None,
    hints: Mapping[str, Any] | None = None,
    desired_outcome: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    policy = await inbound_service.match_source_policy(
        session,
        org_id=org_id,
        connection_id=connection_id,
        kind=kind,
        origin=origin,
    )
    if policy is None:
        return {
            "matched_policy_id": None,
            "domain_projection_id": None,
            "would_store_event": True,
            "would_require_ilo": True,
            "would_project_domain_record": False,
            "would_status": inbound_service.STATUS_REVIEW_REQUIRED,
            "reason": "no_matching_source_policy",
        }
    envelope = {
        "kind": kind,
        "origin": origin,
        "payload": dict(payload or {}),
        "summary": summary,
        "hints": dict(hints or {}),
        "desired_outcome": desired_outcome,
        "idempotency_key": idempotency_key,
    }
    try:
        inbound_service._validate_schema_config(policy.schema_config or {}, envelope)
        schema_error = None
    except Exception as exc:
        schema_error = str(exc)
    projection = await inbound_service._projection_for_policy(session, policy)
    projection_error = _dry_run_projection_error(policy, projection, envelope)
    would_assign_projection = (
        projection is not None
        and schema_error is None
        and inbound_service._policy_allows_domain_projection(policy)
    )
    would_project = projection is not None and schema_error is None and projection_error is None
    return {
        "matched_policy_id": str(policy.id),
        "domain_projection_id": str(projection.id) if would_assign_projection else None,
        "would_store_event": True,
        "would_require_ilo": _dry_run_would_require_ilo(
            projection,
            schema_error=schema_error,
            projection_error=projection_error,
        ),
        "would_project_domain_record": would_project,
        "would_status": _dry_run_would_status(
            projection,
            schema_error=schema_error,
            projection_error=projection_error,
            would_project_domain_record=would_project,
        ),
        "schema_error": schema_error,
        "projection_error": projection_error,
    }


async def _replay_event(
    session: AsyncSession,
    *,
    org_id: str,
    row: InboundEventRow,
    include_payload: bool,
) -> dict[str, Any]:
    envelope = _stored_event_envelope(row)
    replay = await _preview_envelope(
        session,
        org_id=org_id,
        connection_id=str(row.connection_id),
        kind=str(envelope.get("kind") or row.kind or "signal"),
        origin=str(envelope.get("origin") or row.origin),
        payload=dict(envelope.get("payload") or row.raw_payload or {}),
        summary=envelope.get("summary"),
        hints=dict(envelope.get("hints") or {}),
        desired_outcome=envelope.get("desired_outcome"),
        idempotency_key=envelope.get("idempotency_key"),
    )
    original = _original_event_decision(row)
    return {
        "event": serialize_event(row, include_payload=include_payload),
        "original": original,
        "replay": replay,
        "changed": {
            "policy_match": original["matched_policy_id"] != replay["matched_policy_id"],
            "domain_projection_match": original["domain_projection_id"] != replay["domain_projection_id"],
            "status": original["status"] != replay["would_status"],
        },
    }


def _stored_event_envelope(row: InboundEventRow) -> dict[str, Any]:
    envelope = _json_dict(row.envelope)
    if envelope:
        return envelope
    normalized = _json_dict(row.normalized_payload)
    if normalized:
        return normalized
    return {
        "kind": row.kind,
        "origin": row.origin,
        "payload": _json_dict(row.raw_payload),
        "summary": None,
        "hints": {},
        "desired_outcome": None,
        "idempotency_key": row.idempotency_key,
    }


def _original_event_decision(row: InboundEventRow) -> dict[str, Any]:
    return {
        "status": row.status,
        "matched_policy_id": str(row.policy_id) if row.policy_id else None,
        "domain_projection_id": str(row.domain_projection_id) if row.domain_projection_id else None,
        "action_type": row.action_type,
        "error": row.error,
    }


def _summarize_replay(results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    would_statuses: dict[str, int] = {}
    changed = {
        "policy_match": 0,
        "domain_projection_match": 0,
        "status": 0,
    }
    would_require_ilo = 0
    would_project_domain_record = 0
    for result in results:
        replay = dict(result.get("replay") or {})
        status = str(replay.get("would_status") or "unknown")
        would_statuses[status] = would_statuses.get(status, 0) + 1
        if replay.get("would_require_ilo"):
            would_require_ilo += 1
        if replay.get("would_project_domain_record"):
            would_project_domain_record += 1
        change_flags = dict(result.get("changed") or {})
        for key in changed:
            if change_flags.get(key):
                changed[key] += 1
    return {
        "would_statuses": would_statuses,
        "would_require_ilo": would_require_ilo,
        "would_project_domain_record": would_project_domain_record,
        "changed": changed,
    }


async def _build_source_card(
    session: AsyncSession,
    *,
    org_id: str,
    connection: ExternalAgentConnectionRow,
    limit: int | None,
    manual: Mapping[str, Any] | None = None,
    refreshed: bool = False,
) -> dict[str, Any]:
    events = await list_events(
        session,
        org_id=org_id,
        connection_id=str(connection.id),
        limit=limit,
    )
    policies = await list_policies(
        session,
        org_id=org_id,
        connection_id=str(connection.id),
        include_disabled=True,
        limit=READ_LIMIT_MAX,
    )
    projections = await list_projections(
        session,
        org_id=org_id,
        connection_id=str(connection.id),
        include_disabled=True,
        limit=READ_LIMIT_MAX,
    )
    manual = _json_dict(manual)
    return {
        "version": 1,
        "generated_at": _iso(inbound_service.utcnow()),
        "refreshed": bool(refreshed),
        "connection": _source_card_connection(connection),
        "purpose": manual.get("purpose"),
        "notes": manual.get("notes"),
        "tags": _stripped_strings(_json_list(manual.get("tags"))),
        "configured_rules": {
            "policy_count": len(policies),
            "projection_count": len(projections),
            "policies": [_source_card_policy(row) for row in policies],
            "projections": [_source_card_projection(row) for row in projections],
        },
        "traffic": _source_card_traffic(events),
    }


def _source_card_manual(
    metadata: Mapping[str, Any],
    *,
    purpose: str | None,
    notes: str | None,
    tags: Sequence[str] | None,
) -> dict[str, Any]:
    existing = _json_dict(metadata.get("source_card_manual"))
    if purpose is not None:
        existing["purpose"] = str(purpose).strip() or None
    if notes is not None:
        existing["notes"] = str(notes).strip() or None
    if tags is not None:
        existing["tags"] = _stripped_strings(tags)
    return {key: value for key, value in existing.items() if value not in (None, "", [])}


def _source_card_connection(row: ExternalAgentConnectionRow) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "display_name": row.display_name,
        "agent_kind": row.agent_kind,
        "transport": row.transport,
        "status": row.status,
        "capabilities": _json_dict(row.capabilities),
        "last_seen_at": _iso(row.last_seen_at),
        "last_tested_at": _iso(row.last_tested_at),
        "last_error": row.last_error,
    }


def _source_card_policy(row: InboundSourcePolicyRow) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "name": row.name,
        "enabled": bool(row.enabled),
        "priority": int(row.priority),
        "origin_patterns": _json_list(row.origin_patterns),
        "envelope_kinds": _json_list(row.envelope_kinds),
        "allowed_actions": _json_list(row.allowed_actions),
        "auto_execute_actions": _json_list(row.auto_execute_actions),
        "review_mode": row.review_mode,
        "has_instructions": bool(str(row.instructions or "").strip()),
        "schema_required_paths": _schema_required_paths(row.schema_config),
    }


def _source_card_projection(row: InboundDomainProjectionRow) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "policy_id": str(row.policy_id) if row.policy_id else None,
        "enabled": bool(row.enabled),
        "domain_id": row.domain_id,
        "object_key": row.object_key,
        "external_id_path": row.external_id_path,
        "external_id_field": row.external_id_field,
        "field_mapping_keys": sorted(str(key) for key in _json_dict(row.field_mapping)),
        "title_path": row.title_path,
        "upsert_mode": row.upsert_mode,
        "validation_failure_status": row.validation_failure_status,
    }


def _schema_required_paths(schema_config: Mapping[str, Any] | None) -> list[str]:
    config = _json_dict(schema_config)
    paths = _stripped_strings(_json_list(config.get("required_paths")))
    for field in _json_list(config.get("fields")):
        field_config = _json_dict(field)
        if field_config.get("required"):
            path = str(field_config.get("path") or field_config.get("field") or "").strip()
            if path:
                paths.append(path)
    return sorted(set(paths))


def _source_card_traffic(events: Sequence[InboundEventRow]) -> dict[str, Any]:
    status_counts = Counter(str(row.status or "unknown") for row in events)
    origin_counts = Counter(str(row.origin or "unknown") for row in events)
    shape_counts: Counter[str] = Counter()
    for row in events:
        for path in _payload_shape_paths(_json_dict(row.raw_payload)):
            shape_counts[path] += 1
    return {
        "event_count_sampled": len(events),
        "common_origins": _counter_rows(origin_counts),
        "statuses": _counter_rows(status_counts),
        "payload_shapes": _counter_rows(shape_counts, limit=30),
        "observed_outcomes": _source_card_observed_outcomes(events),
        "recent_attention": [_source_card_event_summary(row) for row in events if _event_needs_attention(row)][:10],
        "recent_failures": [_source_card_event_summary(row) for row in events if _event_failed(row)][:10],
        "recent_events": [_source_card_event_summary(row) for row in events[:10]],
    }


def _source_card_observed_outcomes(events: Sequence[InboundEventRow]) -> dict[str, Any]:
    by_origin: dict[str, dict[str, Any]] = {}
    tag_counts: Counter[str] = Counter()
    tool_counts: Counter[str] = Counter()
    total = 0
    for row in events:
        attribution = _event_attribution(row)
        if not attribution:
            continue
        total += 1
        origin = str(row.origin or "unknown")
        bucket = by_origin.setdefault(
            origin,
            {
                "origin": origin,
                "count": 0,
                "tags": Counter(),
                "tool_names": Counter(),
                "summaries": [],
                "recent_event_ids": [],
            },
        )
        bucket["count"] += 1
        bucket["recent_event_ids"].append(str(row.id))
        summary = str(attribution.get("summary") or "").strip()
        if summary and summary not in bucket["summaries"] and len(bucket["summaries"]) < 5:
            bucket["summaries"].append(summary)
        for tag in _stripped_strings(_json_list(attribution.get("tags"))):
            tag_counts[tag] += 1
            bucket["tags"][tag] += 1
        for tool_name in _stripped_strings(_json_list(attribution.get("tool_names"))):
            tool_counts[tool_name] += 1
            bucket["tool_names"][tool_name] += 1

    origins = []
    for bucket in sorted(by_origin.values(), key=lambda item: (-int(item["count"]), item["origin"]))[:10]:
        origins.append(
            {
                "origin": bucket["origin"],
                "count": bucket["count"],
                "tags": _counter_rows(bucket["tags"]),
                "tool_names": _counter_rows(bucket["tool_names"]),
                "summaries": bucket["summaries"],
                "recent_event_ids": bucket["recent_event_ids"][:10],
            }
        )
    return {
        "event_count_sampled": total,
        "common_tags": _counter_rows(tag_counts),
        "tool_names": _counter_rows(tool_counts),
        "by_origin": origins,
    }


def _event_attribution(row: InboundEventRow) -> dict[str, Any]:
    action_result = _json_dict(row.action_result)
    triage = _json_dict(action_result.get("triage"))
    attribution = _json_dict(triage.get("attribution"))
    return attribution or _json_dict(action_result.get("attribution"))


def _counter_rows(counter: Counter[str], *, limit: int = 10) -> list[dict[str, Any]]:
    return [
        {"value": value, "count": count}
        for value, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:limit]
    ]


def _attention_summary(events: Sequence[InboundEventRow]) -> dict[str, Any]:
    return {
        "event_count": len(events),
        "statuses": _counter_rows(Counter(str(row.status or "unknown") for row in events)),
        "origins": _counter_rows(Counter(str(row.origin or "unknown") for row in events)),
        "connections": _counter_rows(Counter(str(row.connection_id) for row in events)),
        "oldest_created_at": _iso(min((row.created_at for row in events), default=None)),
        "newest_created_at": _iso(max((row.created_at for row in events), default=None)),
    }


def _payload_shape_paths(value: Any, *, prefix: str = "payload", depth: int = 0) -> list[str]:
    if depth >= 4:
        return [prefix]
    if isinstance(value, Mapping):
        paths: list[str] = []
        for key in sorted(str(item) for item in value.keys()):
            child = value.get(key)
            child_prefix = f"{prefix}.{key}" if prefix else key
            if isinstance(child, Mapping | list):
                paths.extend(_payload_shape_paths(child, prefix=child_prefix, depth=depth + 1))
            else:
                paths.append(child_prefix)
        return paths or [prefix]
    if isinstance(value, list):
        if not value:
            return [f"{prefix}[]"]
        paths: list[str] = []
        for item in value[:3]:
            paths.extend(_payload_shape_paths(item, prefix=f"{prefix}[]", depth=depth + 1))
        return paths
    return [prefix]


def _event_needs_attention(row: InboundEventRow) -> bool:
    return row.status in ATTENTION_EVENT_STATUSES or bool(row.error)


def _event_failed(row: InboundEventRow) -> bool:
    return row.status in {
        inbound_service.STATUS_FAILED,
        inbound_service.STATUS_QUARANTINED,
    } or bool(row.error)


def _source_card_event_summary(row: InboundEventRow) -> dict[str, Any]:
    serialized = serialize_event(row)
    summary = {
        "id": str(row.id),
        "origin": row.origin,
        "kind": row.kind,
        "status": row.status,
        "policy_id": str(row.policy_id) if row.policy_id else None,
        "domain_projection_id": str(row.domain_projection_id) if row.domain_projection_id else None,
        "action_type": row.action_type,
        "error": serialized["error"],
        "created_at": _iso(row.created_at),
        "processed_at": _iso(row.processed_at),
    }
    if "failure" in serialized:
        summary["failure"] = serialized["failure"]
    return summary


def _dry_run_projection_error(
    policy: InboundSourcePolicyRow,
    projection: InboundDomainProjectionRow | None,
    envelope: Mapping[str, Any],
) -> str | None:
    if projection is None:
        return None
    if not inbound_service._policy_allows_domain_projection(policy):
        return "domain_projection_not_allowed"
    root = inbound_service._path_root(envelope)
    value = inbound_service._extract_path(root, projection.external_id_path)
    external_id = inbound_service._string_value(value)
    if not external_id:
        return f"Missing projection external id at '{projection.external_id_path}'"
    return None


def _dry_run_would_require_ilo(
    projection: InboundDomainProjectionRow | None,
    *,
    schema_error: str | None,
    projection_error: str | None,
) -> bool:
    if schema_error is not None:
        return False
    if projection is None:
        return True
    if projection_error is None:
        return False
    if projection_error == "domain_projection_not_allowed":
        return True
    status = projection.validation_failure_status
    if status not in inbound_service.VALID_PROJECTION_FAILURE_STATUSES:
        status = inbound_service.STATUS_REVIEW_REQUIRED
    return status == inbound_service.STATUS_REVIEW_REQUIRED


def _dry_run_would_status(
    projection: InboundDomainProjectionRow | None,
    *,
    schema_error: str | None,
    projection_error: str | None,
    would_project_domain_record: bool,
) -> str:
    if schema_error is not None:
        return inbound_service.STATUS_QUARANTINED
    if would_project_domain_record:
        return inbound_service.STATUS_PROCESSED
    if projection is None:
        return inbound_service.STATUS_REVIEW_REQUIRED
    if projection_error == "domain_projection_not_allowed":
        return inbound_service.STATUS_REVIEW_REQUIRED
    status = projection.validation_failure_status
    if status not in inbound_service.VALID_PROJECTION_FAILURE_STATUSES:
        return inbound_service.STATUS_REVIEW_REQUIRED
    return status


def serialize_connection(row: ExternalAgentConnectionRow) -> dict[str, Any]:
    return external_agents.serialize_connection(row)


def serialize_token(row: ExternalAgentConnectionTokenRow, *, token: str | None = None) -> dict[str, Any]:
    data = external_agents.serialize_token(row)
    if token is not None:
        data["token"] = token
        data["token_note"] = "Token appears once. Store it in the external source's secret store."
    return data


def serialize_policy(row: InboundSourcePolicyRow) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "org_id": str(row.org_id),
        "connection_id": str(row.connection_id),
        "name": row.name,
        "enabled": bool(row.enabled),
        "priority": int(row.priority),
        "origin_patterns": _json_list(row.origin_patterns),
        "envelope_kinds": _json_list(row.envelope_kinds),
        "instructions": row.instructions,
        "schema_config": _json_dict(row.schema_config),
        "allowed_actions": _json_list(row.allowed_actions),
        "auto_execute_actions": _json_list(row.auto_execute_actions),
        "auto_execute_min_confidence": row.auto_execute_min_confidence,
        "review_mode": row.review_mode,
        "metadata": _json_dict(row.metadata_),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


def serialize_projection(row: InboundDomainProjectionRow) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "org_id": str(row.org_id),
        "connection_id": str(row.connection_id),
        "policy_id": str(row.policy_id) if row.policy_id else None,
        "domain_id": row.domain_id,
        "object_key": row.object_key,
        "enabled": bool(row.enabled),
        "external_id_path": row.external_id_path,
        "external_id_field": row.external_id_field,
        "field_mapping": _json_dict(row.field_mapping),
        "title_path": row.title_path,
        "upsert_mode": row.upsert_mode,
        "validation_failure_status": row.validation_failure_status,
        "metadata": _json_dict(row.metadata_),
        "created_at": _iso(row.created_at),
        "updated_at": _iso(row.updated_at),
    }


_RUN_FAILURE_STATUSES = frozenset({"failed", "canceled", "expired"})


def _direct_public_failure_from_payload(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    payload = dict(value)
    stored_failure = _json_dict(payload.get("failure"))
    status = str(
        stored_failure.get("status")
        or payload.get("run_status")
        or (
            payload.get("status")
            if stored_failure or payload.get("run_id") or payload.get("failure_category")
            else ""
        )
        or ""
    ).strip()
    if status in _RUN_FAILURE_STATUSES:
        return public_run_failure(
            status,
            stored_failure.get("category")
            or payload.get("failure_category")
            or payload.get("category"),
        )
    return None


def _public_failure_from_payload(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    payload = dict(value)
    direct = _direct_public_failure_from_payload(payload)
    if direct is not None:
        return direct
    for key in ("triage", "handling", "result"):
        nested = _public_failure_from_payload(payload.get(key))
        if nested is not None:
            return nested
    return None


def _public_run_outcome(
    value: Any,
    inherited_failure: dict[str, str] | None = None,
) -> dict[str, Any]:
    payload = _json_dict(value)
    projected = dict(payload)
    failure = (
        _direct_public_failure_from_payload(payload)
        or inherited_failure
        or _public_failure_from_payload(payload)
    )
    for key in ("triage", "handling", "result"):
        if isinstance(payload.get(key), dict):
            projected[key] = _public_run_outcome(payload[key], failure)

    if failure is None:
        return projected
    for key in ("final_answer", "error", "reason", "failure_category", "category"):
        projected.pop(key, None)
    projected["failure"] = failure
    return projected


def serialize_event(row: InboundEventRow, *, include_payload: bool = False) -> dict[str, Any]:
    action_result = _public_run_outcome(row.action_result)
    failure = _public_failure_from_payload(action_result)
    data = {
        "id": str(row.id),
        "org_id": str(row.org_id),
        "connection_id": str(row.connection_id),
        "token_id": str(row.token_id) if row.token_id else None,
        "policy_id": str(row.policy_id) if row.policy_id else None,
        "domain_projection_id": str(row.domain_projection_id) if row.domain_projection_id else None,
        "kind": row.kind,
        "origin": row.origin,
        "idempotency_key": row.idempotency_key,
        "status": row.status,
        "confidence": row.confidence,
        "action_type": row.action_type,
        "action_result": action_result,
        "error": failure["message"] if failure is not None else row.error,
        "source_actor": _json_dict(row.source_actor),
        "authority_user_id": str(row.authority_user_id) if row.authority_user_id else None,
        "created_at": _iso(row.created_at),
        "processed_at": _iso(row.processed_at),
    }
    if failure is not None:
        data["failure"] = failure
    if include_payload:
        data.update(
            {
                "raw_payload": _json_dict(row.raw_payload),
                "normalized_payload": _json_dict(row.normalized_payload),
                "envelope": _json_dict(row.envelope),
                "ingress_context": _json_dict(row.ingress_context),
            }
        )
    return data


def serialize_receipt(row: InboundDecisionReceiptRow) -> dict[str, Any]:
    raw_outcome = _json_dict(row.outcome)
    raw_tool_use = _json_dict(row.tool_use)
    failure = (
        _public_failure_from_payload(raw_outcome)
        or _public_failure_from_payload(raw_tool_use)
    )
    outcome = _public_run_outcome(raw_outcome, failure)
    tool_use = _public_run_outcome(raw_tool_use, failure)
    data = {
        "id": str(row.id),
        "event_id": str(row.event_id),
        "org_id": str(row.org_id),
        "connection_id": str(row.connection_id),
        "policy_id": str(row.policy_id) if row.policy_id else None,
        "status": row.status,
        "outcome": outcome,
        "confidence": row.confidence,
        "target": _json_dict(row.target),
        "tool_use": tool_use,
        "reasoning_summary": None if failure is not None else row.reasoning_summary,
        "reusable_pattern_candidate": _json_dict(row.reusable_pattern_candidate),
        "created_at": _iso(row.created_at),
    }
    if failure is not None:
        data["failure"] = failure
    return data
