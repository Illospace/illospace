"""Domains orchestration tool handlers."""

from __future__ import annotations

from brain.systems.runs.tool_catalog.handlers.common import *


def _domain_context() -> tuple[str | None, str | None, int | None, str | None]:
    run = getattr(_agent_context, "run", None)
    execution_metadata = getattr(_agent_context, "execution_metadata", {}) or {}
    org_id = getattr(_agent_context, "org_id", None) or execution_metadata.get("org_id")
    user_id = getattr(_agent_context, "user_id", None) or execution_metadata.get("user_id")
    run_id = getattr(run, "run_id", None) or execution_metadata.get("run_id")
    idea_id = getattr(_agent_context, "idea_id", None) or execution_metadata.get("idea_id")
    return org_id, user_id, run_id, idea_id


def _handle_manage_domain(
    action: str,
    operation: str | None = None,
    domain_id: int | None = None,
    name: str | None = None,
    slug: str | None = None,
    description: str | None = None,
    objects: list[dict] | None = None,
    fields: list[dict] | None = None,
    relations: list[dict] | None = None,
    object_key: str | None = None,
    field: dict | None = None,
    relation_type: dict | None = None,
    search: str | None = None,
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
) -> str:
    action = str(action or "").strip().lower()
    if action == "help":
        return _manage_tool_guide("manage_domain", operation)
    if action == "schema" and (operation or domain_id is None):
        return _manage_tool_guide("manage_domain", operation or "schema")

    from brain.systems.user_domains.service import DomainError, DomainNotFound
    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    org_id, user_id, run_id, idea_id = _domain_context()
    if not org_id:
        return json.dumps({"error": "manage_domain requires an org-scoped run"})

    try:
        with UnitOfWork() as uow:
            service = uow.domains
            actor_id = str(user_id) if user_id else None
            actor_kind = "human" if user_id else "agent"

            if action == "list":
                domains = [
                    service.serialize_domain_summary(domain)
                    for domain in service.list_domains(org_id, include_archived=include_archived)
                ]
                return json.dumps({"domains": domains}, default=str)

            if action == "create_domain":
                if not name:
                    return json.dumps({"error": "create_domain requires: name"})
                domain = service.create_domain(
                    org_id,
                    name=name,
                    slug=slug,
                    description=description,
                    objects=objects or [],
                    relations=relations or [],
                    actor_id=actor_id,
                    actor_kind=actor_kind,
                )
                return json.dumps({"domain": service.serialize_domain_schema(domain)}, default=str)

            if domain_id is None:
                return json.dumps({"error": f"{action} requires: domain_id"})

            domain = service.get_domain(org_id, domain_id, include_archived=include_archived)

            if action == "schema":
                return json.dumps({"domain": service.serialize_domain_schema(domain)}, default=str)

            if action == "remove_domain":
                result = service.remove_domain(
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
                obj = service.add_object_type(
                    domain,
                    payload,
                    actor_id=actor_id,
                    actor_kind=actor_kind,
                )
                return json.dumps({"object": service.serialize_object_type(obj)}, default=str)

            if action == "add_field":
                if not object_key or not field:
                    return json.dumps({"error": "add_field requires: object_key, field"})
                obj = service.get_object_type(domain.id, object_key)
                added = service.add_field_definition(obj, field)
                return json.dumps({"field": service.serialize_field(added)}, default=str)

            if action == "add_relation_type":
                if not relation_type:
                    return json.dumps({"error": "add_relation_type requires: relation_type"})
                added = service.add_relation_type(
                    domain,
                    relation_type,
                    actor_id=actor_id,
                    actor_kind=actor_kind,
                )
                return json.dumps({"relation_type": service.serialize_relation_type(added)}, default=str)

            if action == "query_records":
                records = [
                    service.serialize_record(record)
                    for record in service.list_records(
                        org_id,
                        domain.id,
                        object_key=object_key,
                        search=search,
                        include_archived=include_archived,
                        limit=limit,
                    )
                ]
                return json.dumps({"records": records}, default=str)

            if action == "get_record":
                if record_id is None:
                    return json.dumps({"error": "get_record requires: record_id"})
                record = service.get_record(org_id, domain.id, record_id)
                return json.dumps({"record": service.serialize_record(record)}, default=str)

            if action == "create_record":
                if not object_key:
                    return json.dumps({"error": "create_record requires: object_key"})
                record = service.create_record(
                    org_id,
                    domain.id,
                    object_key,
                    data=data or {},
                    title=title,
                    actor_id=actor_id,
                    actor_kind=actor_kind,
                    run_id=run_id,
                    idea_id=idea_id,
                )
                return json.dumps({"record": service.serialize_record(record)}, default=str)

            if action == "update_record":
                if record_id is None:
                    return json.dumps({"error": "update_record requires: record_id"})
                record = service.update_record(
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
                )
                return json.dumps({"record": service.serialize_record(record)}, default=str)

            if action == "remove_record":
                if record_id is None:
                    return json.dumps({"error": "remove_record requires: record_id"})
                result = service.remove_record(
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
                relation = service.create_relation(
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
                return json.dumps({"relation": service.serialize_relation(relation)}, default=str)

            if action == "events":
                events = [
                    service.serialize_event(event)
                    for event in service.list_events(
                        org_id,
                        domain.id,
                        record_id=record_id,
                        limit=limit,
                    )
                ]
                return json.dumps({"events": events}, default=str)

            return json.dumps({"error": f"Unknown action: {action}"})
    except (DomainError, DomainNotFound) as exc:
        return json.dumps({"error": str(exc)})
    except Exception as exc:
        logger.exception("manage_domain failed: %s", exc)
        return json.dumps({"error": str(exc)})


__all__ = [name for name in globals() if not name.startswith("__")]
