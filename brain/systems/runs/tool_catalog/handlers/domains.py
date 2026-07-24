"""Domains orchestration tool handlers."""

from __future__ import annotations

from brain.kernel.common.pagination import next_offset_token, page_offset
from brain.systems.runs.tool_catalog.handlers.common import *


def _domain_context() -> tuple[str | None, str | None, int | None, str | None]:
    run = getattr(_agent_context, "run", None)
    execution_metadata = getattr(_agent_context, "execution_metadata", {}) or {}
    org_id = getattr(_agent_context, "org_id", None) or execution_metadata.get("org_id")
    user_id = getattr(_agent_context, "user_id", None) or execution_metadata.get("user_id")
    run_id = getattr(run, "run_id", None) or execution_metadata.get("run_id")
    idea_id = getattr(_agent_context, "idea_id", None) or execution_metadata.get("idea_id")
    return org_id, user_id, run_id, idea_id


async def _handle_manage_domain(
    action: str,
    operation: str | None = None,
    domain_id: int | None = None,
    name: str | None = None,
    slug: str | None = None,
    description: str | None = None,
    objects: list[dict] | None = None,
    fields: list[dict] | list[str] | None = None,
    relations: list[dict] | None = None,
    object_key: str | None = None,
    field: dict | None = None,
    relation_type: dict | None = None,
    search: str | None = None,
    format: str = "full",
    order: str = "updated_desc",
    limit: int = 50,
    include_archived: bool = False,
    record_id: int | None = None,
    data: dict | None = None,
    data_patch: dict | None = None,
    title: str | None = None,
    expected_version: int | None = None,
    mode: str = "archive",
    relation_key: str | None = None,
    source_record_id: int | None = None,
    target_record_id: int | None = None,
    properties: dict | None = None,
    cursor: str | None = None,
    confirm_schema_change: bool = False,
) -> str:
    action = str(action or "").strip().lower()
    if action == "help":
        return _manage_tool_guide("manage_domain", operation)
    if action == "schema" and (operation or domain_id is None):
        return _manage_tool_guide("manage_domain", operation or "schema")

    from brain.platform.db.repositories.unit_of_work import UnitOfWork
    from brain.systems.user_domains.service import (
        AsyncDomainService,
        DomainError,
        DomainFieldTypeError,
        DomainNotFound,
        DomainUnknownFieldsError,
    )

    org_id, user_id, run_id, idea_id = _domain_context()
    if not org_id:
        return json.dumps({"error": "manage_domain could not access this workspace context"})

    if action == "create_domain" and confirm_schema_change is not True:
        if not name:
            return json.dumps({"error": "create_domain requires: name"})
        return json.dumps(
            {
                "status": "proposal",
                "created": False,
                "proposal": {
                    "action": "create_domain",
                    "name": name,
                    "slug": slug,
                    "description": description,
                    "objects": objects or [],
                    "relations": relations or [],
                },
                "requires_confirmation": True,
                "confirmation_parameter": "confirm_schema_change",
                "message": (
                    "No Domain was created. Creating a Domain is a workspace schema change, not a "
                    "filing side effect. Present this proposal to the user; set confirm_schema_change=true "
                    "only when the current request explicitly authorizes the new Domain. For filing or "
                    "intake, use a suitable existing Domain (the workspace's default tracker when applicable)."
                ),
            },
            default=str,
        )

    try:
        async with UnitOfWork() as uow:
            service = AsyncDomainService(uow.session)
            actor_id = str(user_id) if user_id else None
            actor_kind = "human" if user_id else "agent"

            if action == "list":
                domains = [
                    await service.serialize_domain_summary(domain)
                    for domain in await service.list_domains(org_id, include_archived=include_archived)
                ]
                return json.dumps({"domains": domains}, default=str)

            if action == "create_domain":
                if not name:
                    return json.dumps({"error": "create_domain requires: name"})
                domain = await service.create_domain(
                    org_id,
                    name=name,
                    slug=slug,
                    description=description,
                    objects=objects or [],
                    relations=relations or [],
                    actor_id=actor_id,
                    actor_kind=actor_kind,
                )
                return json.dumps({"domain": await service.serialize_domain_schema(domain)}, default=str)

            if domain_id is None:
                return json.dumps({"error": f"{action} requires: domain_id"})

            domain = await service.get_domain(org_id, domain_id, include_archived=include_archived)

            if action == "schema":
                return json.dumps({"domain": await service.serialize_domain_schema(domain)}, default=str)

            if action == "remove_domain":
                result = await service.remove_domain(
                    org_id,
                    domain.id,
                    mode=mode,
                    actor_id=actor_id,
                    actor_kind=actor_kind,
                    run_id=run_id,
                    idea_id=idea_id,
                )
                return json.dumps(result, default=str)

            if action == "add_object":
                payload = {
                    "key": object_key,
                    "name": name,
                    "description": description,
                    "fields": fields or [],
                }
                if not object_key:
                    return json.dumps({"error": "add_object requires: object_key"})
                obj = await service.add_object_type(
                    domain,
                    payload,
                    actor_id=actor_id,
                    actor_kind=actor_kind,
                )
                return json.dumps({"object": await service.serialize_object_type(obj)}, default=str)

            if action == "add_field":
                if not object_key or not field:
                    return json.dumps({"error": "add_field requires: object_key, field"})
                obj = await service.get_object_type(domain.id, object_key)
                added = await service.add_field_definition(obj, field)
                return json.dumps({"field": service.serialize_field(added)}, default=str)

            if action == "add_relation_type":
                if not relation_type:
                    return json.dumps({"error": "add_relation_type requires: relation_type"})
                added = await service.add_relation_type(
                    domain,
                    relation_type,
                    actor_id=actor_id,
                    actor_kind=actor_kind,
                )
                return json.dumps({"relation_type": await service.serialize_relation_type(added)}, default=str)

            if action == "query_records":
                page_limit = max(1, min(int(limit or 50), 500))
                page_kind = f"manage_domain:records:{domain.id}"
                offset = page_offset(cursor, kind=page_kind)
                record_format = str(format or "full").strip().lower()
                if record_format not in {"full", "compact"}:
                    raise DomainError("format must be 'full' or 'compact'")
                record_order = str(order or "updated_desc").strip().lower()
                compact_fields = None
                if record_format == "compact" and fields is not None:
                    if not all(isinstance(item, str) for item in fields):
                        raise DomainError("fields must contain only strings for format='compact'")
                    compact_fields = fields
                record_rows = await service.list_records(
                    org_id,
                    domain.id,
                    object_key=object_key,
                    search=search,
                    include_archived=include_archived,
                    limit=page_limit,
                    order=record_order,
                    offset=offset,
                )
                if record_format == "compact":
                    records = [
                        await service.serialize_record_compact(record, fields=compact_fields)
                        for record in record_rows
                    ]
                else:
                    records = [await service.serialize_record(record) for record in record_rows]
                total = await service.count_records(
                    org_id,
                    domain.id,
                    object_key=object_key,
                    search=search,
                    include_archived=include_archived,
                )
                has_more = offset + len(records) < total
                return json.dumps(
                    {
                        "records": records,
                        "returned": len(records),
                        "total_matching": total,
                        "order": record_order,
                        "format": record_format,
                        "truncated": has_more,
                        "next_page": (
                            next_offset_token(
                                kind=page_kind,
                                offset=offset,
                                returned=len(records),
                            )
                            if has_more
                            else None
                        ),
                        "evidence_health": {
                            "status": "ok",
                            "completeness": "more_available" if has_more else "complete",
                        },
                    },
                    default=str,
                )

            if action == "get_record":
                if record_id is None:
                    return json.dumps({"error": "get_record requires: record_id"})
                record = await service.get_record(org_id, domain.id, record_id)
                return json.dumps({"record": await service.serialize_record(record)}, default=str)

            if action == "create_record":
                if not object_key:
                    return json.dumps({"error": "create_record requires: object_key"})
                warnings: list[dict] = []
                record = await service.create_record(
                    org_id,
                    domain.id,
                    object_key,
                    data=data or {},
                    title=title,
                    actor_id=actor_id,
                    actor_kind=actor_kind,
                    run_id=run_id,
                    idea_id=idea_id,
                    allow_partial=True,
                    partial_warnings=warnings,
                )
                payload = {"record": await service.serialize_record(record)}
                if warnings:
                    payload["warnings"] = warnings
                return json.dumps(payload, default=str)

            if action == "update_record":
                if record_id is None:
                    return json.dumps({"error": "update_record requires: record_id"})
                warnings = []
                record = await service.update_record(
                    org_id,
                    domain.id,
                    record_id,
                    data_patch=data_patch or {},
                    title=title,
                    expected_version=expected_version,
                    actor_id=actor_id,
                    actor_kind=actor_kind,
                    run_id=run_id,
                    idea_id=idea_id,
                    allow_partial=True,
                    partial_warnings=warnings,
                )
                payload = {"record": await service.serialize_record(record)}
                if warnings:
                    payload["warnings"] = warnings
                return json.dumps(payload, default=str)

            if action == "remove_record":
                if record_id is None:
                    return json.dumps({"error": "remove_record requires: record_id"})
                result = await service.remove_record(
                    org_id,
                    domain.id,
                    record_id,
                    mode=mode,
                    actor_id=actor_id,
                    actor_kind=actor_kind,
                    run_id=run_id,
                    idea_id=idea_id,
                )
                return json.dumps(result, default=str)

            if action == "link_records":
                if not relation_key or source_record_id is None or target_record_id is None:
                    return json.dumps(
                        {"error": "link_records requires: relation_key, source_record_id, target_record_id"}
                    )
                relation = await service.create_relation(
                    org_id,
                    domain.id,
                    relation_key,
                    source_record_id=source_record_id,
                    target_record_id=target_record_id,
                    properties=properties or {},
                    actor_id=actor_id,
                    actor_kind=actor_kind,
                    run_id=run_id,
                    idea_id=idea_id,
                )
                return json.dumps({"relation": await service.serialize_relation(relation)}, default=str)

            if action == "events":
                page_limit = max(1, min(int(limit or 50), 200))
                page_kind = f"manage_domain:events:{domain.id}"
                offset = page_offset(cursor, kind=page_kind)
                events = [
                    service.serialize_event(event)
                    for event in await service.list_events(
                        org_id,
                        domain.id,
                        record_id=record_id,
                        limit=page_limit,
                        offset=offset,
                    )
                ]
                total = await service.count_events(
                    org_id,
                    domain.id,
                    record_id=record_id,
                )
                has_more = offset + len(events) < total
                return json.dumps(
                    {
                        "events": events,
                        "returned": len(events),
                        "total_matching": total,
                        "truncated": has_more,
                        "next_page": (
                            next_offset_token(
                                kind=page_kind,
                                offset=offset,
                                returned=len(events),
                            )
                            if has_more
                            else None
                        ),
                        "evidence_health": {
                            "status": "ok",
                            "completeness": "more_available" if has_more else "complete",
                        },
                    },
                    default=str,
                )

            return json.dumps({"error": f"Unknown action: {action}"})
    except DomainFieldTypeError as exc:
        return json.dumps(
            {
                "error": str(exc),
                "error_code": exc.code,
                "field": "field_type",
                "received": exc.field_type,
                "allowed_values": list(exc.allowed_values),
            }
        )
    except DomainUnknownFieldsError as exc:
        return json.dumps(
            {
                "error": str(exc),
                "error_code": exc.code,
                "unknown_fields": list(exc.unknown_fields),
                "valid_fields": list(exc.valid_fields),
            }
        )
    except (DomainError, DomainNotFound) as exc:
        return json.dumps({"error": str(exc)})
    except Exception as exc:
        logger.exception("manage_domain failed: %s", exc)
        return json.dumps({"error": str(exc)})


async def _handle_merge_chantier(
    duplicate_record_id: int,
    canonical_record_id: int,
    expected_duplicate_version: int,
    expected_canonical_version: int,
    reason: str,
) -> str:
    """Run the first-class duplicate retirement operation in Domain 1."""

    from brain.platform.db.repositories.unit_of_work import UnitOfWork
    from brain.systems.chantiers import merge_chantier_records
    from brain.systems.user_domains.service import (
        AsyncDomainService,
        DomainNotFound,
    )

    org_id, user_id, run_id, _idea_id = _domain_context()
    if not org_id:
        return json.dumps({"error": "merge_chantier could not access this workspace context"})

    try:
        async with UnitOfWork() as uow:
            result = await merge_chantier_records(
                uow.session,
                org_id=org_id,
                duplicate_record_id=int(duplicate_record_id),
                canonical_record_id=int(canonical_record_id),
                expected_duplicate_version=int(expected_duplicate_version),
                expected_canonical_version=int(expected_canonical_version),
                reason=reason,
                actor_user_id=str(user_id) if user_id else None,
                run_id=run_id,
            )
            service = AsyncDomainService(uow.session)
            return json.dumps(
                {
                    "status": result.status,
                    "domain_id": result.domain_id,
                    "canonical": await service.serialize_record(result.canonical),
                    "duplicate": await service.serialize_record(result.duplicate),
                    "active_chantier_count": result.active_chantier_count,
                    "digest_record_ids": list(result.active_record_ids),
                },
                default=str,
            )
    except (TypeError, ValueError, DomainNotFound) as exc:
        return json.dumps({"error": str(exc)})
    except Exception as exc:
        logger.exception("merge_chantier failed: %s", exc)
        return json.dumps({"error": str(exc)})


__all__ = [name for name in globals() if not name.startswith("__")]
