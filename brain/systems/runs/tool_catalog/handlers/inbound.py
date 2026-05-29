"""Inbound coordination admin tool handlers."""

from __future__ import annotations

from datetime import datetime
import json
from typing import Any

from brain.systems.runs.tool_catalog.handlers.common import *


def _inbound_context() -> tuple[str | None, str | None]:
    execution_metadata = getattr(_agent_context, "execution_metadata", {}) or {}
    org_id = getattr(_agent_context, "org_id", None) or execution_metadata.get("org_id")
    user_id = getattr(_agent_context, "user_id", None) or execution_metadata.get("user_id")
    return (str(org_id) if org_id else None, str(user_id) if user_id else None)


def _parse_datetime(value: Any):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("expires_at must be an ISO datetime") from exc


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, default=str)


def _error(message: str) -> str:
    return _dump({"error": message})


async def _handle_manage_inbound(
    action: str,
    operation: str | None = None,
    connection_id: str | None = None,
    display_name: str | None = None,
    agent_kind: str | None = None,
    transport: str | None = None,
    endpoint_url: str | None = None,
    remote_agent_id: str | None = None,
    remote_agent_card: dict | None = None,
    capabilities: dict | None = None,
    metadata: dict | None = None,
    status: str | None = None,
    include_disabled: bool = False,
    include_revoked: bool = False,
    token_id: str | None = None,
    token_name: str | None = None,
    token_scopes: list[str] | None = None,
    expires_at: str | None = None,
    policy_id: str | None = None,
    name: str | None = None,
    enabled: bool | None = None,
    priority: int | None = None,
    origin_patterns: list[str] | None = None,
    envelope_kinds: list[str] | None = None,
    instructions: str | None = None,
    schema_config: dict | None = None,
    allowed_actions: list[str] | None = None,
    auto_execute_actions: list[str] | None = None,
    auto_execute_min_confidence: float | None = None,
    review_mode: str | None = None,
    projection_id: str | None = None,
    domain_id: int | None = None,
    object_key: str | None = None,
    external_id_path: str | None = None,
    external_id_field: str | None = None,
    field_mapping: dict | None = None,
    title_path: str | None = None,
    upsert_mode: str | None = None,
    validation_failure_status: str | None = None,
    auto_allow_policy_action: bool = True,
    event_id: str | None = None,
    origin: str | None = None,
    kind: str = "signal",
    payload: dict | None = None,
    include_payload: bool = False,
    include_receipts: bool = False,
    source_purpose: str | None = None,
    source_notes: str | None = None,
    source_tags: list[str] | None = None,
    limit: int = 25,
) -> str:
    action = str(action or "").strip().lower()
    if action in {"help", "schema"}:
        return _manage_tool_guide("manage_inbound", operation)

    from brain.platform.db.repositories.unit_of_work import UnitOfWork
    from brain.systems.external_agents import service as external_agents
    from brain.systems.inbound import admin as inbound_admin

    org_id, user_id = _inbound_context()
    if not org_id:
        return _error("manage_inbound could not access this workspace context")

    try:
        async with UnitOfWork() as uow:
            session = uow.session

            if action == "list_connections":
                rows = await inbound_admin.list_connections(
                    session,
                    org_id=org_id,
                    agent_kind=agent_kind,
                    transport=transport,
                    include_disabled=include_disabled,
                    limit=limit,
                )
                return _dump({"connections": [inbound_admin.serialize_connection(row) for row in rows]})

            if action == "get_connection":
                if not connection_id:
                    return _error("get_connection requires: connection_id")
                row = await inbound_admin.require_connection_for_org(
                    session,
                    org_id=org_id,
                    connection_id=connection_id,
                )
                return _dump({"connection": inbound_admin.serialize_connection(row)})

            if action == "create_connection":
                if not user_id:
                    return _error("create_connection requires user context")
                if not display_name:
                    return _error("create_connection requires: display_name")
                row = await inbound_admin.create_connection(
                    session,
                    org_id=org_id,
                    owner_user_id=user_id,
                    display_name=display_name,
                    agent_kind=agent_kind or "custom",
                    transport=transport or "webhook",
                    endpoint_url=endpoint_url,
                    remote_agent_id=remote_agent_id,
                    remote_agent_card=remote_agent_card,
                    capabilities=capabilities,
                    metadata=metadata,
                )
                return _dump({"connection": inbound_admin.serialize_connection(row)})

            if action == "update_connection":
                if not connection_id:
                    return _error("update_connection requires: connection_id")
                row = await inbound_admin.update_connection(
                    session,
                    org_id=org_id,
                    connection_id=connection_id,
                    display_name=display_name,
                    status=status,
                    endpoint_url=endpoint_url,
                    remote_agent_id=remote_agent_id,
                    remote_agent_card=remote_agent_card,
                    capabilities=capabilities,
                    metadata=metadata,
                )
                return _dump({"connection": inbound_admin.serialize_connection(row)})

            if action == "mint_token":
                if not connection_id:
                    return _error("mint_token requires: connection_id")
                raw_token, row = await inbound_admin.mint_signal_token(
                    session,
                    org_id=org_id,
                    connection_id=connection_id,
                    name=token_name or "Inbound signal token",
                    scopes=token_scopes,
                    expires_at=_parse_datetime(expires_at),
                )
                return _dump({"token": inbound_admin.serialize_token(row, token=raw_token)})

            if action == "list_tokens":
                rows = await inbound_admin.list_tokens(
                    session,
                    org_id=org_id,
                    connection_id=connection_id,
                    include_revoked=include_revoked,
                    limit=limit,
                )
                return _dump({"tokens": [inbound_admin.serialize_token(row) for row in rows]})

            if action == "get_token":
                if not token_id:
                    return _error("get_token requires: token_id")
                row = await inbound_admin.require_token_for_org(
                    session,
                    org_id=org_id,
                    token_id=token_id,
                )
                return _dump({"token": inbound_admin.serialize_token(row)})

            if action == "revoke_token":
                if not token_id:
                    return _error("revoke_token requires: token_id")
                row = await inbound_admin.revoke_token(session, org_id=org_id, token_id=token_id)
                return _dump({"token": inbound_admin.serialize_token(row)})

            if action == "list_policies":
                rows = await inbound_admin.list_policies(
                    session,
                    org_id=org_id,
                    connection_id=connection_id,
                    include_disabled=include_disabled,
                    limit=limit,
                )
                return _dump({"policies": [inbound_admin.serialize_policy(row) for row in rows]})

            if action == "get_policy":
                if not policy_id:
                    return _error("get_policy requires: policy_id")
                row = await inbound_admin.require_policy_for_org(session, org_id=org_id, policy_id=policy_id)
                return _dump({"policy": inbound_admin.serialize_policy(row)})

            if action == "create_policy":
                if not connection_id or not name or origin_patterns is None:
                    return _error("create_policy requires: connection_id, name, origin_patterns")
                min_confidence = 0.85 if auto_execute_min_confidence is None else auto_execute_min_confidence
                row = await inbound_admin.create_policy(
                    session,
                    org_id=org_id,
                    connection_id=connection_id,
                    name=name,
                    origin_patterns=origin_patterns,
                    priority=priority if priority is not None else 100,
                    envelope_kinds=envelope_kinds,
                    instructions=instructions,
                    schema_config=schema_config,
                    allowed_actions=allowed_actions,
                    auto_execute_actions=auto_execute_actions,
                    auto_execute_min_confidence=min_confidence,
                    review_mode=review_mode or "review_required",
                    metadata=metadata,
                    enabled=True if enabled is None else enabled,
                )
                return _dump({"policy": inbound_admin.serialize_policy(row)})

            if action == "update_policy":
                if not policy_id:
                    return _error("update_policy requires: policy_id")
                row = await inbound_admin.update_policy(
                    session,
                    org_id=org_id,
                    policy_id=policy_id,
                    name=name,
                    enabled=enabled,
                    priority=priority,
                    origin_patterns=origin_patterns,
                    envelope_kinds=envelope_kinds,
                    instructions=instructions,
                    schema_config=schema_config,
                    allowed_actions=allowed_actions,
                    auto_execute_actions=auto_execute_actions,
                    auto_execute_min_confidence=auto_execute_min_confidence,
                    review_mode=review_mode,
                    metadata=metadata,
                )
                return _dump({"policy": inbound_admin.serialize_policy(row)})

            if action == "list_projections":
                rows = await inbound_admin.list_projections(
                    session,
                    org_id=org_id,
                    connection_id=connection_id,
                    policy_id=policy_id,
                    include_disabled=include_disabled,
                    limit=limit,
                )
                return _dump({"projections": [inbound_admin.serialize_projection(row) for row in rows]})

            if action == "get_projection":
                if not projection_id:
                    return _error("get_projection requires: projection_id")
                row = await inbound_admin.require_projection_for_org(
                    session,
                    org_id=org_id,
                    projection_id=projection_id,
                )
                return _dump({"projection": inbound_admin.serialize_projection(row)})

            if action == "create_projection":
                required = {
                    "connection_id": connection_id,
                    "domain_id": domain_id,
                    "object_key": object_key,
                    "external_id_path": external_id_path,
                    "external_id_field": external_id_field,
                    "field_mapping": field_mapping,
                }
                missing = [key for key, value in required.items() if value in (None, "", {})]
                if missing:
                    return _error(f"create_projection requires: {', '.join(missing)}")
                row = await inbound_admin.create_projection(
                    session,
                    org_id=org_id,
                    connection_id=str(connection_id),
                    policy_id=policy_id,
                    domain_id=int(domain_id),
                    object_key=str(object_key),
                    external_id_path=str(external_id_path),
                    external_id_field=str(external_id_field),
                    field_mapping=field_mapping or {},
                    title_path=title_path,
                    upsert_mode=upsert_mode or "upsert",
                    validation_failure_status=validation_failure_status or "review_required",
                    metadata=metadata,
                    enabled=True if enabled is None else enabled,
                    auto_allow_policy_action=auto_allow_policy_action,
                )
                return _dump({"projection": inbound_admin.serialize_projection(row)})

            if action == "update_projection":
                if not projection_id:
                    return _error("update_projection requires: projection_id")
                row = await inbound_admin.update_projection(
                    session,
                    org_id=org_id,
                    projection_id=projection_id,
                    policy_id=policy_id,
                    domain_id=domain_id,
                    object_key=object_key,
                    enabled=enabled,
                    external_id_path=external_id_path,
                    external_id_field=external_id_field,
                    field_mapping=field_mapping,
                    title_path=title_path,
                    upsert_mode=upsert_mode,
                    validation_failure_status=validation_failure_status,
                    metadata=metadata,
                )
                return _dump({"projection": inbound_admin.serialize_projection(row)})

            if action == "list_events":
                rows = await inbound_admin.list_events(
                    session,
                    org_id=org_id,
                    connection_id=connection_id,
                    policy_id=policy_id,
                    status=status,
                    origin=origin,
                    limit=limit,
                )
                return _dump({
                    "events": [
                        inbound_admin.serialize_event(row, include_payload=include_payload)
                        for row in rows
                    ]
                })

            if action == "list_attention_events":
                result = await inbound_admin.list_attention_events(
                    session,
                    org_id=org_id,
                    connection_id=connection_id,
                    policy_id=policy_id,
                    origin=origin,
                    limit=limit,
                    include_payload=include_payload,
                )
                return _dump(result)

            if action == "get_event":
                if not event_id:
                    return _error("get_event requires: event_id")
                row = await inbound_admin.require_event_for_org(session, org_id=org_id, event_id=event_id)
                data = {"event": inbound_admin.serialize_event(row, include_payload=True)}
                if include_receipts:
                    receipts = await inbound_admin.list_receipts(
                        session,
                        org_id=org_id,
                        event_id=str(row.id),
                        limit=limit,
                    )
                    data["receipts"] = [inbound_admin.serialize_receipt(receipt) for receipt in receipts]
                return _dump(data)

            if action == "list_receipts":
                rows = await inbound_admin.list_receipts(
                    session,
                    org_id=org_id,
                    event_id=event_id,
                    limit=limit,
                )
                return _dump({"receipts": [inbound_admin.serialize_receipt(row) for row in rows]})

            if action == "dry_run_match":
                if not connection_id or not origin:
                    return _error("dry_run_match requires: connection_id, origin")
                result = await inbound_admin.dry_run_match(
                    session,
                    org_id=org_id,
                    connection_id=connection_id,
                    origin=origin,
                    kind=kind or "signal",
                    payload=payload,
                )
                return _dump({"dry_run": result})

            if action == "replay_events":
                result = await inbound_admin.replay_events(
                    session,
                    org_id=org_id,
                    event_id=event_id,
                    connection_id=connection_id,
                    policy_id=policy_id,
                    status=status,
                    origin=origin,
                    limit=limit,
                    include_payload=include_payload,
                )
                return _dump({"replay": result})

            if action == "get_source_card":
                if not connection_id:
                    return _error("get_source_card requires: connection_id")
                result = await inbound_admin.get_source_card(
                    session,
                    org_id=org_id,
                    connection_id=connection_id,
                    limit=limit,
                )
                return _dump(result)

            if action == "refresh_source_card":
                if not connection_id:
                    return _error("refresh_source_card requires: connection_id")
                result = await inbound_admin.refresh_source_card(
                    session,
                    org_id=org_id,
                    connection_id=connection_id,
                    purpose=source_purpose,
                    notes=source_notes,
                    tags=source_tags,
                    limit=limit,
                )
                return _dump(result)

            return _error(f"Unknown action: {action}")
    except (
        inbound_admin.InboundAdminError,
        external_agents.ExternalAgentError,
        ValueError,
    ) as exc:
        return _error(str(exc))
    except Exception as exc:
        logger.exception("manage_inbound failed: %s", exc)
        return _error(str(exc))


__all__ = [name for name in globals() if not name.startswith("__")]
