"""Shared inbound signal processing service."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.domain import DomainRecord
from brain.platform.db.models.external_agent import ExternalAgentConnectionRow
from brain.platform.db.models.idea import Idea, IdeaThread, ThreadContextSubmission
from brain.platform.db.models.inbound import (
    InboundDecisionReceiptRow,
    InboundDomainProjectionKeyRow,
    InboundDomainProjectionRow,
    InboundEventRow,
    InboundSourcePolicyRow,
)
from brain.systems.external_agents import service as external_agents
from brain.systems.inbound.status import (
    STATUS_FAILED,
    STATUS_PROCESSED,
    STATUS_QUARANTINED,
    STATUS_REVIEW_REQUIRED,
)
from brain.systems.runs.work_intake import WorkIntakeEvent, admit_work
from brain.systems.user_domains.service import AsyncDomainService, DomainError, DomainNotFound


ACTION_STORE_ONLY = "store_only"
ACTION_DOMAIN_PROJECTION_UPSERT = "domain_projection.upsert"
ACTION_ILO_REQUIRED = "ilo_required"
ACTION_THREAD_ATTACHED = "thread.attached"
ACTION_CONTEXT_ACCEPTED = "accepted"
CONTEXT_ENVELOPE_KIND = "context"

DOMAIN_PROJECTION_ACTIONS = frozenset(
    {ACTION_DOMAIN_PROJECTION_UPSERT, "domain_projection", "create_domain_record"}
)
VALID_PROJECTION_FAILURE_STATUSES = frozenset(
    {STATUS_REVIEW_REQUIRED, STATUS_QUARANTINED, STATUS_FAILED}
)
VALID_PROJECTION_UPSERT_MODES = frozenset({"upsert", "create_only", "update_only"})
MAX_INBOUND_KIND_LENGTH = 40
MAX_INBOUND_ORIGIN_LENGTH = 240
MAX_TRIAGE_MESSAGE_CHARS = 8000
MAX_TRIAGE_PAYLOAD_CHARS = 5000
DEFAULT_PUBLIC_APP_URL = "http://localhost:8080"


class InboundValidationError(ValueError):
    """Raised when an inbound envelope or configured projection is invalid."""


@dataclass(frozen=True)
class _ConnectionContext:
    connection_id: str
    org_id: str
    owner_user_id: str | None
    token_id: str | None
    scopes: frozenset[str] | None
    display_name: str | None
    source_kind: str | None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def create_source_policy(
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
    review_mode: str = STATUS_REVIEW_REQUIRED,
    metadata: Mapping[str, Any] | None = None,
    enabled: bool = True,
) -> InboundSourcePolicyRow:
    """Create an Illo-configurable source policy record."""

    policy = InboundSourcePolicyRow(
        org_id=str(org_id),
        connection_id=str(connection_id),
        name=_nonempty(name, "name"),
        enabled=bool(enabled),
        priority=int(priority),
        origin_patterns=[_nonempty(pattern, "origin pattern") for pattern in origin_patterns],
        envelope_kinds=[str(kind or "signal").strip() for kind in (envelope_kinds or ["signal"])],
        instructions=_clean_optional(instructions),
        schema_config=dict(schema_config or {}),
        allowed_actions=[str(action).strip() for action in (allowed_actions or []) if str(action).strip()],
        auto_execute_actions=[
            str(action).strip() for action in (auto_execute_actions or []) if str(action).strip()
        ],
        auto_execute_min_confidence=float(auto_execute_min_confidence),
        review_mode=str(review_mode or STATUS_REVIEW_REQUIRED),
        metadata_=dict(metadata or {}),
    )
    session.add(policy)
    await session.flush()
    return policy


async def create_domain_projection(
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
    validation_failure_status: str = STATUS_REVIEW_REQUIRED,
    metadata: Mapping[str, Any] | None = None,
    enabled: bool = True,
) -> InboundDomainProjectionRow:
    """Create a configured deterministic Domain Projection."""

    projection = InboundDomainProjectionRow(
        org_id=str(org_id),
        connection_id=str(connection_id),
        policy_id=str(policy_id) if policy_id else None,
        domain_id=int(domain_id),
        object_key=_nonempty(object_key, "object_key"),
        enabled=bool(enabled),
        external_id_path=_nonempty(external_id_path, "external_id_path"),
        external_id_field=_nonempty(external_id_field, "external_id_field"),
        field_mapping={str(key): str(value) for key, value in dict(field_mapping).items()},
        title_path=_clean_optional(title_path),
        upsert_mode=str(upsert_mode or "upsert"),
        validation_failure_status=str(validation_failure_status or STATUS_REVIEW_REQUIRED),
        metadata_=dict(metadata or {}),
    )
    session.add(projection)
    await session.flush()
    return projection


async def submit_inbound_envelope(
    session: AsyncSession,
    *,
    connection: external_agents.AgentBridgePrincipal | ExternalAgentConnectionRow | Mapping[str, Any],
    envelope: Mapping[str, Any],
    ingress_context: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Store and process a shared inbound envelope from any ingress lane."""

    context = await _resolve_connection_context(session, connection)
    _require_signal_scope(context)
    normalized = _normalize_envelope(envelope)
    existing = await _find_idempotent_event(session, context, normalized.get("idempotency_key"))
    if existing is not None:
        return _result_from_event(existing, idempotent_replay=True)

    event = InboundEventRow(
        org_id=context.org_id,
        connection_id=context.connection_id,
        token_id=context.token_id,
        kind=normalized["kind"],
        origin=normalized["origin"],
        idempotency_key=normalized.get("idempotency_key"),
        raw_payload=dict(normalized["payload"]),
        normalized_payload=normalized,
        envelope=normalized,
        ingress_context=dict(ingress_context or {}),
        source_actor=_source_actor(context),
        authority_user_id=context.owner_user_id,
        status="received",
    )
    event, idempotent_replay = await _store_inbound_event(session, context, event)
    if idempotent_replay:
        return _result_from_event(event, idempotent_replay=True)

    policy: InboundSourcePolicyRow | None = None
    projection: InboundDomainProjectionRow | None = None
    try:
        if normalized["kind"] == CONTEXT_ENVELOPE_KIND:
            return await _process_context_envelope(
                session,
                context=context,
                event=event,
                normalized=normalized,
            )

        policy = await match_source_policy(
            session,
            org_id=context.org_id,
            connection_id=context.connection_id,
            kind=normalized["kind"],
            origin=normalized["origin"],
        )
        if policy is None:
            return await _complete_event_with_illo_triage(
                session,
                event,
                context=context,
                normalized=normalized,
                policy=None,
                status=STATUS_REVIEW_REQUIRED,
                action_type=ACTION_ILO_REQUIRED,
                action_result={"reason": "no_matching_source_policy"},
                confidence=None,
                reasoning_summary="No active source policy matched this inbound signal.",
            )

        event.policy_id = str(policy.id)
        _validate_schema_config(policy.schema_config or {}, normalized)
        projection = await _projection_for_policy(session, policy)
        if projection is None:
            return await _complete_event_with_illo_triage(
                session,
                event,
                context=context,
                normalized=normalized,
                policy=policy,
                status=STATUS_REVIEW_REQUIRED,
                action_type=ACTION_ILO_REQUIRED,
                action_result={"reason": "matched_policy_without_projection"},
                confidence=None,
                reasoning_summary="Source policy matched, but no deterministic projection is configured.",
            )
        if not _policy_allows_domain_projection(policy):
            return await _complete_event_with_illo_triage(
                session,
                event,
                context=context,
                normalized=normalized,
                policy=policy,
                status=STATUS_REVIEW_REQUIRED,
                action_type=ACTION_ILO_REQUIRED,
                action_result={"reason": "domain_projection_not_allowed"},
                confidence=None,
                reasoning_summary="Source policy matched a projection, but the policy does not allow projection writes.",
            )

        event.domain_projection_id = str(projection.id)
        action_result = await _apply_domain_projection(
            session,
            context=context,
            event=event,
            envelope=normalized,
            projection=projection,
        )
        return await _complete_event(
            session,
            event,
            policy=policy,
            status=STATUS_PROCESSED,
            action_type=ACTION_DOMAIN_PROJECTION_UPSERT,
            action_result=action_result,
            confidence=1.0,
            target={
                "domain_id": projection.domain_id,
                "object_key": projection.object_key,
                "record_id": action_result.get("record_id"),
            },
            tool_use={"type": ACTION_DOMAIN_PROJECTION_UPSERT},
            reasoning_summary="Configured Domain Projection handled this signal deterministically.",
            reusable_pattern_candidate={"origin": normalized["origin"], "policy_id": str(policy.id)},
        )
    except (DomainError, DomainNotFound, InboundValidationError) as exc:
        status = projection.validation_failure_status if projection is not None else STATUS_QUARANTINED
        if status not in VALID_PROJECTION_FAILURE_STATUSES:
            status = STATUS_REVIEW_REQUIRED
        if status == STATUS_REVIEW_REQUIRED:
            return await _complete_event_with_illo_triage(
                session,
                event,
                context=context,
                normalized=normalized,
                policy=policy,
                status=status,
                action_type=ACTION_ILO_REQUIRED,
                action_result={"reason": "validation_error"},
                confidence=None,
                error=str(exc),
                reasoning_summary=str(exc),
            )
        return await _complete_event(
            session,
            event,
            policy=policy,
            status=status,
            action_type=ACTION_DOMAIN_PROJECTION_UPSERT if projection is not None else ACTION_STORE_ONLY,
            action_result={"reason": "validation_error"},
            confidence=None,
            error=str(exc),
            reasoning_summary=str(exc),
        )
    except Exception as exc:
        return await _complete_event(
            session,
            event,
            policy=policy,
            status=STATUS_FAILED,
            action_type=event.action_type or ACTION_STORE_ONLY,
            action_result={"reason": "processing_failed"},
            confidence=None,
            error=str(exc),
            reasoning_summary=str(exc),
        )


async def match_source_policy(
    session: AsyncSession,
    *,
    org_id: str,
    connection_id: str,
    kind: str,
    origin: str,
) -> InboundSourcePolicyRow | None:
    """Return the first active policy whose kind and origin patterns match."""

    stmt = (
        select(InboundSourcePolicyRow)
        .where(
            InboundSourcePolicyRow.org_id == str(org_id),
            InboundSourcePolicyRow.connection_id == str(connection_id),
            InboundSourcePolicyRow.enabled.is_(True),
        )
        .order_by(
            InboundSourcePolicyRow.priority.asc(),
            InboundSourcePolicyRow.created_at.asc(),
            InboundSourcePolicyRow.id.asc(),
        )
    )
    policies = (await session.scalars(stmt)).all()
    for policy in policies:
        kinds = {str(item or "").strip() for item in _as_list(policy.envelope_kinds)}
        if kinds and "*" not in kinds and str(kind) not in kinds:
            continue
        if _origin_matches(origin, policy.origin_patterns):
            return policy
    return None


async def _resolve_connection_context(
    session: AsyncSession,
    connection: external_agents.AgentBridgePrincipal | ExternalAgentConnectionRow | Mapping[str, Any],
) -> _ConnectionContext:
    if isinstance(connection, external_agents.AgentBridgePrincipal):
        return _ConnectionContext(
            connection_id=str(connection.connection_id),
            org_id=str(connection.org_id),
            owner_user_id=str(connection.owner_user_id),
            token_id=str(connection.token_id),
            scopes=frozenset(connection.scopes),
            display_name=str(connection.connection_display_name),
            source_kind=str(connection.agent_kind),
        )

    if isinstance(connection, ExternalAgentConnectionRow):
        return _ConnectionContext(
            connection_id=str(connection.id),
            org_id=str(connection.org_id),
            owner_user_id=str(connection.owner_user_id),
            token_id=None,
            scopes=None,
            display_name=str(connection.display_name),
            source_kind=str(connection.agent_kind),
        )

    data = dict(connection)
    connection_id = str(data.get("connection_id") or data.get("id") or "").strip()
    org_id = str(data.get("org_id") or "").strip()
    owner_user_id = _clean_optional(data.get("owner_user_id") or data.get("authority_user_id"))
    display_name = _clean_optional(data.get("display_name") or data.get("connection_display_name"))
    source_kind = _clean_optional(data.get("agent_kind") or data.get("source_kind"))
    if connection_id and (not org_id or not owner_user_id or not display_name or not source_kind):
        row = await session.get(ExternalAgentConnectionRow, connection_id)
        if row is not None:
            org_id = org_id or str(row.org_id)
            owner_user_id = owner_user_id or str(row.owner_user_id)
            display_name = display_name or str(row.display_name)
            source_kind = source_kind or str(row.agent_kind)
    if not connection_id or not org_id:
        raise InboundValidationError("connection_id and org_id are required")
    raw_scopes = data.get("scopes")
    scopes = frozenset(str(scope) for scope in _as_list(raw_scopes)) if raw_scopes is not None else None
    return _ConnectionContext(
        connection_id=connection_id,
        org_id=org_id,
        owner_user_id=owner_user_id,
        token_id=_clean_optional(data.get("token_id")),
        scopes=scopes,
        display_name=display_name,
        source_kind=source_kind,
    )


def _require_signal_scope(context: _ConnectionContext) -> None:
    if context.scopes is None:
        return
    required = getattr(external_agents, "SCOPE_SIGNAL_SUBMIT", "signal:submit")
    if "*" not in context.scopes and required not in context.scopes:
        raise external_agents.ExternalAgentPermissionError(f"Bridge token is missing scope: {required}")


def _normalize_envelope(envelope: Mapping[str, Any]) -> dict[str, Any]:
    data = dict(envelope or {})
    kind = str(data.get("kind") or "signal").strip()
    origin = str(data.get("origin") or "").strip()
    if not kind:
        raise InboundValidationError("kind is required")
    if not origin:
        raise InboundValidationError("origin is required")
    if len(kind) > MAX_INBOUND_KIND_LENGTH:
        raise InboundValidationError(f"kind must be {MAX_INBOUND_KIND_LENGTH} characters or fewer")
    if len(origin) > MAX_INBOUND_ORIGIN_LENGTH:
        raise InboundValidationError(f"origin must be {MAX_INBOUND_ORIGIN_LENGTH} characters or fewer")
    payload = data.get("payload") or {}
    if not isinstance(payload, dict):
        raise InboundValidationError("payload must be an object")
    hints = data.get("hints") or {}
    if not isinstance(hints, dict):
        raise InboundValidationError("hints must be an object")
    normalized = {
        "kind": kind,
        "origin": origin,
        "payload": dict(payload),
        "summary": _clean_optional(data.get("summary")),
        "hints": dict(hints),
        "desired_outcome": _clean_optional(data.get("desired_outcome")),
        "idempotency_key": _clean_optional(data.get("idempotency_key")),
    }
    if kind == CONTEXT_ENVELOPE_KIND:
        normalized.update(_normalize_context_fields(data, normalized["payload"]))
    if normalized["idempotency_key"] and len(str(normalized["idempotency_key"])) > 160:
        raise InboundValidationError("idempotency_key must be 160 characters or fewer")
    return normalized


def _normalize_context_fields(data: Mapping[str, Any], payload: Mapping[str, Any]) -> dict[str, Any]:
    intent = _clean_optional(data.get("intent")) or _clean_optional(payload.get("intent"))
    source = data.get("source", payload.get("source", {}))
    constraints = data.get("constraints", payload.get("constraints", {}))
    correlation = data.get("correlation", payload.get("correlation", {}))
    parts = data.get("parts", payload.get("parts", []))
    if source is None:
        source = {}
    if constraints is None:
        constraints = {}
    if correlation is None:
        correlation = {}
    if parts is None:
        parts = []
    if not isinstance(source, dict):
        raise InboundValidationError("source must be an object")
    if not isinstance(constraints, dict):
        raise InboundValidationError("constraints must be an object")
    if not isinstance(correlation, dict):
        raise InboundValidationError("correlation must be an object")
    if not isinstance(parts, list):
        raise InboundValidationError("parts must be an array")
    if not intent and not parts and not _clean_optional(data.get("summary")):
        raise InboundValidationError("context envelope requires intent, summary, or parts")
    return {
        "intent": intent,
        "source": dict(source),
        "constraints": dict(constraints),
        "correlation": dict(correlation),
        "parts": list(parts),
    }


async def _find_idempotent_event(
    session: AsyncSession,
    context: _ConnectionContext,
    idempotency_key: str | None,
) -> InboundEventRow | None:
    if not idempotency_key:
        return None
    stmt = (
        select(InboundEventRow)
        .where(
            InboundEventRow.connection_id == context.connection_id,
            InboundEventRow.idempotency_key == idempotency_key,
        )
        .limit(1)
    )
    return (await session.scalars(stmt)).first()


async def _store_inbound_event(
    session: AsyncSession,
    context: _ConnectionContext,
    event: InboundEventRow,
) -> tuple[InboundEventRow, bool]:
    try:
        async with session.begin_nested():
            session.add(event)
            await session.flush()
    except IntegrityError:
        existing = await _find_idempotent_event(session, context, event.idempotency_key)
        if existing is not None:
            return existing, True
        raise
    return event, False


async def _projection_for_policy(
    session: AsyncSession,
    policy: InboundSourcePolicyRow,
) -> InboundDomainProjectionRow | None:
    stmt = (
        select(InboundDomainProjectionRow)
        .where(
            InboundDomainProjectionRow.policy_id == str(policy.id),
            InboundDomainProjectionRow.enabled.is_(True),
        )
        .order_by(InboundDomainProjectionRow.created_at.asc(), InboundDomainProjectionRow.id.asc())
        .limit(1)
    )
    return (await session.scalars(stmt)).first()


async def _apply_domain_projection(
    session: AsyncSession,
    *,
    context: _ConnectionContext,
    event: InboundEventRow,
    envelope: Mapping[str, Any],
    projection: InboundDomainProjectionRow,
) -> dict[str, Any]:
    if projection.upsert_mode not in VALID_PROJECTION_UPSERT_MODES:
        raise InboundValidationError("projection upsert_mode must be upsert, create_only, or update_only")

    root = _path_root(envelope)
    external_id = _string_value(_extract_path(root, projection.external_id_path))
    if not external_id:
        raise InboundValidationError(f"Missing projection external id at '{projection.external_id_path}'")

    data = {projection.external_id_field: external_id}
    for field_key, source_path in dict(projection.field_mapping or {}).items():
        value = _extract_path(root, str(source_path))
        if value is _MISSING:
            continue
        data[str(field_key)] = value

    title = None
    if projection.title_path:
        title_value = _extract_path(root, projection.title_path)
        title = _string_value(title_value) if title_value is not _MISSING else None

    domain_service = AsyncDomainService(session)
    projection_key = await _get_projection_key(session, projection, external_id=external_id)
    existing = await _record_from_projection_key(session, projection_key, context.org_id, projection)
    if projection_key is not None and existing is None:
        existing = await _find_projected_record(domain_service, context.org_id, projection, external_id=external_id)
        if existing is not None:
            projection_key.record_id = existing.id
            await session.flush()
    if projection_key is None:
        existing = await _find_projected_record(domain_service, context.org_id, projection, external_id=external_id)
        if existing is not None:
            projection_key, _claimed = await _claim_projection_key(
                session,
                context=context,
                projection=projection,
                external_id=external_id,
                record_id=existing.id,
            )
    if projection_key is None and existing is None:
        projection_key, claimed = await _claim_projection_key(
            session,
            context=context,
            projection=projection,
            external_id=external_id,
        )
        if not claimed:
            existing = await _record_from_projection_key(session, projection_key, context.org_id, projection)
            if existing is None:
                existing = await _find_projected_record(
                    domain_service,
                    context.org_id,
                    projection,
                    external_id=external_id,
                )
                if existing is not None:
                    projection_key.record_id = existing.id
                    await session.flush()
    reason = f"inbound_event:{event.id}"
    if existing is None:
        if projection.upsert_mode == "update_only":
            raise InboundValidationError("Projection is update_only but no existing record matched")
        record = await domain_service.create_record(
            context.org_id,
            projection.domain_id,
            projection.object_key,
            data=data,
            title=title,
            actor_id=context.owner_user_id,
            actor_kind="external_source",
            reason=reason,
        )
        projection_key.record_id = record.id
        await session.flush()
        operation = "created"
    else:
        if projection.upsert_mode == "create_only":
            raise InboundValidationError("Projection is create_only but a record already exists")
        record = await domain_service.update_record(
            context.org_id,
            projection.domain_id,
            existing.id,
            data_patch=data,
            title=title,
            actor_id=context.owner_user_id,
            actor_kind="external_source",
            reason=reason,
        )
        operation = "updated"

    serialized = await domain_service.serialize_record(record)
    return {
        "operation": operation,
        "domain_id": projection.domain_id,
        "object_key": projection.object_key,
        "record_id": record.id,
        "external_id": external_id,
        "projection_key_id": str(projection_key.id) if projection_key is not None else None,
        "record": serialized,
    }


async def _get_projection_key(
    session: AsyncSession,
    projection: InboundDomainProjectionRow,
    *,
    external_id: str,
) -> InboundDomainProjectionKeyRow | None:
    stmt = (
        select(InboundDomainProjectionKeyRow)
        .where(
            InboundDomainProjectionKeyRow.projection_id == str(projection.id),
            InboundDomainProjectionKeyRow.external_id == external_id,
        )
        .limit(1)
        .with_for_update()
    )
    return (await session.scalars(stmt)).first()


async def _claim_projection_key(
    session: AsyncSession,
    *,
    context: _ConnectionContext,
    projection: InboundDomainProjectionRow,
    external_id: str,
    record_id: int | None = None,
) -> tuple[InboundDomainProjectionKeyRow, bool]:
    key = InboundDomainProjectionKeyRow(
        org_id=context.org_id,
        projection_id=str(projection.id),
        domain_id=projection.domain_id,
        record_id=record_id,
        external_id=external_id,
    )
    try:
        async with session.begin_nested():
            session.add(key)
            await session.flush()
    except IntegrityError:
        existing = await _get_projection_key(session, projection, external_id=external_id)
        if existing is not None:
            return existing, False
        raise
    return key, True


async def _record_from_projection_key(
    session: AsyncSession,
    key: InboundDomainProjectionKeyRow | None,
    org_id: str,
    projection: InboundDomainProjectionRow,
) -> DomainRecord | None:
    if key is None or key.record_id is None:
        return None
    record = await session.get(DomainRecord, key.record_id)
    if (
        record is None
        or str(record.org_id) != str(org_id)
        or int(record.domain_id) != int(projection.domain_id)
        or record.archived_at is not None
    ):
        return None
    return record


async def _find_projected_record(
    domain_service: AsyncDomainService,
    org_id: str,
    projection: InboundDomainProjectionRow,
    *,
    external_id: str,
) -> DomainRecord | None:
    domain = await domain_service.get_domain(org_id, projection.domain_id)
    obj = await domain_service.get_object_type(domain.id, projection.object_key)
    external_id_expr = DomainRecord.data[projection.external_id_field].as_string()
    bind = domain_service.session.get_bind()
    if bind.dialect.name == "sqlite":
        external_id_expr = func.json_extract(DomainRecord.data, f"$.{projection.external_id_field}")
    stmt = (
        select(DomainRecord)
        .where(
            DomainRecord.org_id == org_id,
            DomainRecord.domain_id == domain.id,
            DomainRecord.object_type_id == obj.id,
            DomainRecord.archived_at.is_(None),
            external_id_expr == external_id,
        )
        .order_by(DomainRecord.updated_at.desc(), DomainRecord.id.desc())
        .limit(1)
    )
    return (await domain_service.session.scalars(stmt)).first()


def _validate_schema_config(schema_config: Mapping[str, Any], envelope: Mapping[str, Any]) -> None:
    if not schema_config:
        return
    required_paths = list(schema_config.get("required_paths") or [])
    for field in _as_list(schema_config.get("fields")):
        if isinstance(field, Mapping) and field.get("required"):
            path = field.get("path") or field.get("field")
            if path:
                required_paths.append(str(path))
    root = _path_root(envelope)
    missing = [path for path in required_paths if _required_path_missing(_extract_path(root, str(path)))]
    if missing:
        raise InboundValidationError(f"Missing required inbound field(s): {', '.join(sorted(missing))}")


async def _add_receipt(
    session: AsyncSession,
    event: InboundEventRow,
    *,
    policy: InboundSourcePolicyRow | None,
    outcome: Mapping[str, Any],
    confidence: float | None = None,
    target: Mapping[str, Any] | None = None,
    tool_use: Mapping[str, Any] | None = None,
    reasoning_summary: str | None = None,
    reusable_pattern_candidate: Mapping[str, Any] | None = None,
) -> InboundDecisionReceiptRow:
    receipt = InboundDecisionReceiptRow(
        event_id=str(event.id),
        org_id=str(event.org_id),
        connection_id=str(event.connection_id),
        policy_id=str(policy.id) if policy is not None else None,
        status=event.status,
        outcome=_json_safe(dict(outcome)),
        confidence=confidence,
        target=_json_safe(dict(target or {})),
        tool_use=_json_safe(dict(tool_use or {})),
        reasoning_summary=reasoning_summary,
        reusable_pattern_candidate=_json_safe(dict(reusable_pattern_candidate or {})),
    )
    session.add(receipt)
    await session.flush()
    return receipt


async def _complete_event_with_illo_triage(
    session: AsyncSession,
    event: InboundEventRow,
    *,
    context: _ConnectionContext,
    normalized: Mapping[str, Any],
    policy: InboundSourcePolicyRow | None,
    status: str,
    action_type: str,
    action_result: Mapping[str, Any],
    confidence: float | None,
    error: str | None = None,
    reasoning_summary: str | None = None,
    reusable_pattern_candidate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    triage = await _queue_illo_triage(
        session,
        context=context,
        event=event,
        normalized=normalized,
        policy=policy,
        reason=str(action_result.get("reason") or status),
        reasoning_summary=reasoning_summary,
    )
    merged_result = {**dict(action_result), "triage": triage}
    return await _complete_event(
        session,
        event,
        policy=policy,
        status=status,
        action_type=action_type,
        action_result=merged_result,
        confidence=confidence,
        error=error,
        target=_triage_target(triage),
        tool_use=_triage_tool_use(triage),
        reasoning_summary=reasoning_summary,
        reusable_pattern_candidate=reusable_pattern_candidate,
    )


async def _queue_illo_triage(
    session: AsyncSession,
    *,
    context: _ConnectionContext,
    event: InboundEventRow,
    normalized: Mapping[str, Any],
    policy: InboundSourcePolicyRow | None,
    reason: str,
    reasoning_summary: str | None,
) -> dict[str, Any]:
    if not context.owner_user_id:
        return {"status": "skipped", "reason": "missing_authority_user"}

    origin = str(normalized.get("origin") or event.origin)
    source_name = context.display_name or context.source_kind or "external source"
    title = _truncate(f"Inbound signal needs Illo triage: {origin}", 180)
    idea = Idea(
        title=title,
        description=_truncate(
            f"{source_name} sent an inbound signal that needs Illo to decide the workspace outcome.",
            500,
        ),
        status="emerged",
        origin="inbound_signal",
        origin_ref=f"inbound_event:{event.id}",
        user_id=context.owner_user_id,
        org_id=context.org_id,
        agent_details={
            "inbound_triage": {
                "event_id": str(event.id),
                "origin": origin,
                "reason": reason,
                "connection_id": context.connection_id,
                "policy_id": str(policy.id) if policy is not None else None,
            }
        },
    )
    session.add(idea)
    await session.flush()

    message = _triage_thread_message(
        context=context,
        event=event,
        normalized=normalized,
        policy=policy,
        reason=reason,
        reasoning_summary=reasoning_summary,
    )
    thread_message = IdeaThread(
        idea_id=str(idea.id),
        role="user",
        content=message,
        attachments=[],
        metadata_={
            "source": "inbound.triage",
            "event_id": str(event.id),
            "origin": origin,
            "reason": reason,
            "connection_id": context.connection_id,
            "policy_id": str(policy.id) if policy is not None else None,
            "authority_user_id": context.owner_user_id,
        },
        message_type="message",
        user_id=context.owner_user_id,
    )
    session.add(thread_message)
    await session.flush()

    result = await admit_work(
        session,
        WorkIntakeEvent(
            source="inbound",
            event_type="inbound.triage_required",
            org_id=context.org_id,
            actor={
                "id": context.owner_user_id,
                "org_id": context.org_id,
                "principal_type": "external_source_authority",
                "name": source_name,
            },
            target={"kind": "cortex_idea", "idea_id": str(idea.id)},
            payload={
                "message": message,
                "metadata": {
                    "execution_profile": "fast",
                    "thread_message_id": thread_message.id,
                    "inbound_event": _inbound_event_metadata(context, event, normalized, policy),
                },
            },
            policy={
                "producer": "inbound",
                "idempotency_key": f"inbound:triage:{event.id}",
                "run_event": "inbound_triage_required",
            },
        ),
    )

    triage = {
        "status": "queued" if result.ok else "run_admission_failed",
        "idea_id": str(idea.id),
        "thread_message_id": thread_message.id,
        "event_id": str(event.id),
    }
    if result.run_id is not None:
        triage["run_id"] = result.run_id
    if result.skipped_reason:
        triage["error"] = result.skipped_reason
    return triage


def _triage_thread_message(
    *,
    context: _ConnectionContext,
    event: InboundEventRow,
    normalized: Mapping[str, Any],
    policy: InboundSourcePolicyRow | None,
    reason: str,
    reasoning_summary: str | None,
) -> str:
    origin = str(normalized.get("origin") or event.origin)
    lines = [
        "An inbound signal needs Illo triage.",
        "",
        f"Reason: {reason}",
        f"Inbound event: {event.id}",
        f"Origin: {origin}",
        f"Source: {context.display_name or context.source_kind or context.connection_id}",
    ]
    if normalized.get("summary"):
        lines.append(f"Summary: {normalized.get('summary')}")
    if normalized.get("desired_outcome"):
        lines.append(f"Desired outcome from source: {normalized.get('desired_outcome')}")
    if policy is not None:
        lines.append(f"Matched policy: {policy.name} ({policy.id})")
        if policy.instructions:
            lines.extend(["", "Policy instructions:", str(policy.instructions)])
    if reasoning_summary:
        lines.extend(["", "Preflight note:", reasoning_summary])
    hints = normalized.get("hints")
    if hints:
        lines.extend(["", "Hints:", _json_preview(hints, limit=1600)])
    lines.extend(
        [
            "",
            "Payload preview:",
            _json_preview(normalized.get("payload") or {}, limit=MAX_TRIAGE_PAYLOAD_CHARS),
            "",
            "Decide the appropriate IloSpace outcome using the available tools and skills. "
            "A workspace mutation is optional; store, summarize, ask, schedule, or no-op when that is best.",
        ]
    )
    return _truncate("\n".join(lines), MAX_TRIAGE_MESSAGE_CHARS)


def _inbound_event_metadata(
    context: _ConnectionContext,
    event: InboundEventRow,
    normalized: Mapping[str, Any],
    policy: InboundSourcePolicyRow | None,
) -> dict[str, Any]:
    return {
        "event_id": str(event.id),
        "origin": str(normalized.get("origin") or event.origin),
        "kind": str(normalized.get("kind") or event.kind),
        "connection_id": context.connection_id,
        "token_id": context.token_id,
        "display_name": context.display_name,
        "source_kind": context.source_kind,
        "authority_user_id": context.owner_user_id,
        "policy_id": str(policy.id) if policy is not None else None,
        "summary": normalized.get("summary"),
        "desired_outcome": normalized.get("desired_outcome"),
    }


def _triage_target(triage: Mapping[str, Any]) -> dict[str, Any]:
    if not triage.get("idea_id"):
        return {}
    return {
        "kind": "cortex_idea",
        "idea_id": triage.get("idea_id"),
        **({"run_id": triage.get("run_id")} if triage.get("run_id") is not None else {}),
    }


def _triage_tool_use(triage: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "illo_triage",
        "status": triage.get("status"),
        **({"run_id": triage.get("run_id")} if triage.get("run_id") is not None else {}),
    }


async def _process_context_envelope(
    session: AsyncSession,
    *,
    context: _ConnectionContext,
    event: InboundEventRow,
    normalized: Mapping[str, Any],
) -> dict[str, Any]:
    """Store submitted personal-agent context without starting Illo work."""

    if not context.owner_user_id:
        return await _store_context_event(
            session,
            context=context,
            event=event,
            normalized=normalized,
            reason="missing_authority_user",
            message="Context accepted and stored as an inbound event because no authority user was available.",
        )

    correlation = dict(normalized.get("correlation") or {})
    thread = await _correlated_thread(session, context=context, correlation=correlation)
    if thread is None:
        return await _store_context_event(
            session,
            context=context,
            event=event,
            normalized=normalized,
            reason="no_correlated_thread",
            message=(
                "Context accepted and stored. No visible Thread was created because "
                "the submission was not correlated with an existing Thread."
            ),
        )

    return await _attach_context_to_thread(
        session,
        context=context,
        event=event,
        normalized=normalized,
        correlation=correlation,
        thread=thread,
    )


async def _store_context_event(
    session: AsyncSession,
    *,
    context: _ConnectionContext,
    event: InboundEventRow,
    normalized: Mapping[str, Any],
    reason: str,
    message: str,
) -> dict[str, Any]:
    parts = list(normalized.get("parts") or [])
    action_result = {
        "operation": "stored",
        "reason": reason,
        "event_id": str(event.id),
        "origin": normalized.get("origin"),
        "part_count": len(parts),
        "message": message,
    }
    return await _complete_event(
        session,
        event,
        policy=None,
        status=STATUS_PROCESSED,
        action_type=ACTION_CONTEXT_ACCEPTED,
        action_result=action_result,
        confidence=1.0,
        target={"kind": "inbound_event", "event_id": str(event.id)},
        tool_use={"type": "illo_submit_context", "operation": "stored"},
        reasoning_summary=message,
        reusable_pattern_candidate={
            "kind": CONTEXT_ENVELOPE_KIND,
            "origin": normalized.get("origin"),
            "source_kind": context.source_kind,
        },
    )


async def _attach_context_to_thread(
    session: AsyncSession,
    *,
    context: _ConnectionContext,
    event: InboundEventRow,
    normalized: Mapping[str, Any],
    correlation: Mapping[str, Any],
    thread: Idea,
) -> dict[str, Any]:
    operation = "attached"

    source = dict(normalized.get("source") or {})
    constraints = dict(normalized.get("constraints") or {})
    parts = list(normalized.get("parts") or [])
    intent = _clean_optional(normalized.get("intent")) or _clean_optional(normalized.get("summary"))

    submission = ThreadContextSubmission(
        thread_id=str(thread.id),
        org_id=context.org_id,
        source_connection_id=context.connection_id,
        submitted_by_user_id=context.owner_user_id,
        inbound_event_id=str(event.id),
        intent=intent,
        source=source,
        constraints=constraints,
        correlation=correlation,
        parts=_json_safe(parts),
        routing_result={},
    )
    session.add(submission)
    await session.flush()

    thread_message = IdeaThread(
        idea_id=str(thread.id),
        role="user",
        content=_context_timeline_preview(
            context=context,
            normalized=normalized,
            submission_id=str(submission.id),
        ),
        attachments=[],
        metadata_={
            "source": "inbound.context",
            "event_id": str(event.id),
            "context_submission_id": str(submission.id),
            "origin": normalized.get("origin"),
            "operation": operation,
            "connection_id": context.connection_id,
            "source_connection_display_name": context.display_name,
            "source_kind": context.source_kind,
            "part_count": len(parts),
        },
        message_type="context_submission",
        user_id=context.owner_user_id,
    )
    session.add(thread_message)
    await session.flush()

    thread_url = _thread_url(thread)
    thread_route = _thread_route(thread)
    action_result = {
        "operation": operation,
        "thread_id": str(thread.id),
        "thread_url": thread_url,
        "thread_route": thread_route,
        "url": thread_url,
        "context_submission_id": str(submission.id),
        "thread_message_id": thread_message.id,
        "event_id": str(event.id),
        "message": "Context accepted and attached to an existing Thread.",
    }
    submission.routing_result = dict(action_result)
    return await _complete_event(
        session,
        event,
        policy=None,
        status=STATUS_PROCESSED,
        action_type=ACTION_THREAD_ATTACHED,
        action_result=action_result,
        confidence=1.0,
        target={"kind": "cortex_idea", "idea_id": str(thread.id)},
        tool_use={"type": "illo_submit_context", "operation": operation},
        reasoning_summary="Context ingress was routed deterministically by explicit correlation.",
        reusable_pattern_candidate={
            "kind": CONTEXT_ENVELOPE_KIND,
            "origin": normalized.get("origin"),
            "source_kind": context.source_kind,
        },
    )


async def _correlated_thread(
    session: AsyncSession,
    *,
    context: _ConnectionContext,
    correlation: Mapping[str, Any],
) -> Idea | None:
    thread_id = _clean_optional(correlation.get("thread_id") or correlation.get("idea_id"))
    if not thread_id:
        return None
    thread = await session.get(Idea, thread_id)
    if thread is None or str(thread.org_id) != str(context.org_id):
        raise InboundValidationError("correlation.thread_id does not match an accessible Thread")
    return thread


def _context_timeline_preview(
    *,
    context: _ConnectionContext,
    normalized: Mapping[str, Any],
    submission_id: str,
) -> str:
    source_name = context.display_name or context.source_kind or "personal agent"
    parts = list(normalized.get("parts") or [])
    lines = [
        f"Context submitted from {source_name}.",
        "",
        f"Submission: {submission_id}",
    ]
    if normalized.get("intent"):
        lines.append(f"Intent: {normalized.get('intent')}")
    if normalized.get("summary") and normalized.get("summary") != normalized.get("intent"):
        lines.append(f"Summary: {normalized.get('summary')}")
    if normalized.get("origin"):
        lines.append(f"Origin: {normalized.get('origin')}")
    if parts:
        lines.extend(["", f"Context parts: {len(parts)}"])
        for index, part in enumerate(parts[:8], start=1):
            lines.append(f"{index}. {_context_part_preview(part)}")
        if len(parts) > 8:
            lines.append(f"... plus {len(parts) - 8} more part(s).")
    if normalized.get("source"):
        lines.extend(["", f"Source: {_json_preview(normalized.get('source'), limit=600)}"])
    return _truncate("\n".join(lines), MAX_TRIAGE_MESSAGE_CHARS)


def _context_part_preview(part: Any) -> str:
    if not isinstance(part, dict):
        return _truncate(part, 180)
    part_type = _clean_optional(part.get("type") or part.get("kind")) or "part"
    title = _clean_optional(part.get("title") or part.get("name") or part.get("label"))
    text = _clean_optional(part.get("text") or part.get("content") or part.get("url") or part.get("uri"))
    bits = [part_type]
    if title:
        bits.append(title)
    if text:
        bits.append(_truncate(text, 140))
    return " - ".join(bits)


def _public_app_base_url() -> str:
    raw = (
        os.environ.get("ILLO_PUBLIC_URL")
        or os.environ.get("ILLO_DASHBOARD_URL")
        or DEFAULT_PUBLIC_APP_URL
    ).strip()
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return DEFAULT_PUBLIC_APP_URL
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def _thread_route_for_id(thread_id: Any) -> str:
    return f"/cortex?idea={thread_id}"


def _thread_url_for_route(route: str) -> str:
    return f"{_public_app_base_url()}{route}"


def _thread_route(thread: Idea) -> str:
    return _thread_route_for_id(thread.id)


def _thread_url(thread: Idea) -> str:
    return _thread_url_for_route(_thread_route(thread))


def _with_thread_links(outcome: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(outcome or {})
    thread_id = _clean_optional(result.get("thread_id") or result.get("idea_id"))
    if not thread_id:
        return result
    route = _clean_optional(result.get("thread_route"))
    existing_url = _clean_optional(result.get("url"))
    if route is None and existing_url and existing_url.startswith("/"):
        route = existing_url
    route = route or _thread_route_for_id(thread_id)
    thread_url = _clean_optional(result.get("thread_url"))
    if thread_url is None or thread_url.startswith("/"):
        thread_url = _thread_url_for_route(route)
    result["thread_route"] = route
    result["thread_url"] = thread_url
    result["url"] = thread_url
    return result


async def _complete_event(
    session: AsyncSession,
    event: InboundEventRow,
    *,
    policy: InboundSourcePolicyRow | None,
    status: str,
    action_type: str,
    action_result: Mapping[str, Any],
    confidence: float | None,
    error: str | None = None,
    target: Mapping[str, Any] | None = None,
    tool_use: Mapping[str, Any] | None = None,
    reasoning_summary: str | None = None,
    reusable_pattern_candidate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    _finalize_event(
        event,
        status=status,
        action_type=action_type,
        action_result=action_result,
        confidence=confidence,
        error=error,
    )
    await _add_receipt(
        session,
        event,
        policy=policy,
        outcome=event.action_result,
        confidence=confidence,
        target=target,
        tool_use=tool_use,
        reasoning_summary=reasoning_summary,
        reusable_pattern_candidate=reusable_pattern_candidate,
    )
    await session.flush()
    return _result_from_event(event)


def _finalize_event(
    event: InboundEventRow,
    *,
    status: str,
    action_type: str,
    action_result: Mapping[str, Any],
    confidence: float | None,
    error: str | None = None,
) -> None:
    event.status = status
    event.action_type = action_type
    event.action_result = _json_safe(dict(action_result))
    event.confidence = confidence
    event.error = error
    event.processed_at = utcnow()


def _result_from_event(event: InboundEventRow, *, idempotent_replay: bool = False) -> dict[str, Any]:
    outcome = _with_thread_links(event.action_result or {})
    return {
        "status": event.status,
        "event_id": str(event.id),
        "matched_policy_id": str(event.policy_id) if event.policy_id else None,
        "domain_projection_id": str(event.domain_projection_id) if event.domain_projection_id else None,
        "ilo_outcome": outcome,
        "confidence": event.confidence,
        "idempotent_replay": idempotent_replay,
        "error": event.error,
    }


def _source_actor(context: _ConnectionContext) -> dict[str, Any]:
    return {
        "type": "external_source_connection",
        "connection_id": context.connection_id,
        "display_name": context.display_name,
        "source_kind": context.source_kind,
    }


def _origin_matches(origin: str, patterns: Any) -> bool:
    values = [str(pattern).strip() for pattern in _as_list(patterns) if str(pattern).strip()]
    return any(pattern == "*" or fnmatchcase(origin, pattern) for pattern in values)


def _policy_allows_domain_projection(policy: InboundSourcePolicyRow) -> bool:
    actions = {str(action or "").strip() for action in _as_list(policy.allowed_actions)}
    actions.discard("")
    return "*" in actions or bool(actions & DOMAIN_PROJECTION_ACTIONS)


class _Missing:
    pass


_MISSING = _Missing()


def _required_path_missing(value: Any) -> bool:
    if value is _MISSING or value is None:
        return True
    return isinstance(value, str) and not value.strip()


def _path_root(envelope: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "envelope": dict(envelope),
        "payload": dict(envelope.get("payload") or {}),
        "hints": dict(envelope.get("hints") or {}),
        "summary": envelope.get("summary"),
        "desired_outcome": envelope.get("desired_outcome"),
        "origin": envelope.get("origin"),
    }


def _extract_path(root: Mapping[str, Any], path: str) -> Any:
    current: Any = root
    for part in [segment for segment in str(path or "").split(".") if segment]:
        if isinstance(current, Mapping) and part in current:
            current = current[part]
            continue
        if isinstance(current, list) and part.isdigit():
            idx = int(part)
            if 0 <= idx < len(current):
                current = current[idx]
                continue
        return _MISSING
    return current


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple | set | frozenset):
        return list(value)
    return [value]


def _clean_optional(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _nonempty(value: Any, field_name: str) -> str:
    text = _clean_optional(value)
    if text is None:
        raise InboundValidationError(f"{field_name} is required")
    return text


def _string_value(value: Any) -> str | None:
    if value is _MISSING or value is None:
        return None
    text = str(value).strip()
    return text or None


def _json_preview(value: Any, *, limit: int) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str)
    except TypeError:
        text = str(value)
    return _truncate(text, limit)


def _truncate(value: Any, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 3)].rstrip()}..."


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value
