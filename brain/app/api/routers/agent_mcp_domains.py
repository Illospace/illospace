"""Hosted MCP tools for Illo Domains."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from brain.systems.external_agents import service as external_agents
from brain.systems.user_domains.service import AsyncDomainService, DomainNotFound


ToolHandler = Callable[
    [AsyncSession, external_agents.AgentBridgePrincipal, dict[str, Any]],
    Awaitable[dict[str, Any]],
]


def _tool_schema(description: str, properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": required or [],
        },
    }


DOMAIN_MCP_TOOLS: dict[str, dict[str, Any]] = {
    "illo_inspect_domains": {
        **_tool_schema(
            (
                "Inspect Illo Domains, the user-generated managed databases in the workspace. "
                "Without a domain_id or slug, this returns visible domain summaries. With a "
                "domain_id or slug, it returns the domain schema and can optionally include "
                "records, relations, and recent change events for read-only analysis."
            ),
            {
                "domain_id": {"type": "integer", "description": "Optional Domain id to inspect."},
                "slug": {"type": "string", "description": "Optional Domain slug to inspect."},
                "object_key": {
                    "type": "string",
                    "description": "Optional object key used when listing records.",
                },
                "record_id": {
                    "type": "integer",
                    "description": "Optional record id to fetch from the selected Domain.",
                },
                "search": {
                    "type": "string",
                    "description": "Optional text search when listing records.",
                },
                "format": {
                    "type": "string",
                    "enum": ["full", "compact"],
                    "default": "full",
                    "description": "Record serialization used when include_records is true.",
                },
                "fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Data field keys to include when format is compact.",
                },
                "order": {
                    "type": "string",
                    "enum": ["updated_desc", "updated_asc"],
                    "default": "updated_desc",
                    "description": "Record ordering used when include_records is true.",
                },
                "filters": {
                    "type": "object",
                    "description": (
                        "Optional exact record filters keyed by record data field, dotted data path, "
                        "or metadata key such as title/id. Values may be scalars, arrays, or small "
                        "operator objects using eq, in, contains, or exists."
                    ),
                    "default": {},
                },
                "relation_key": {
                    "type": "string",
                    "description": "Optional relation type key used when listing relations.",
                },
                "source_record_id": {
                    "type": "integer",
                    "description": "Optional source record filter for relations.",
                },
                "target_record_id": {
                    "type": "integer",
                    "description": "Optional target record filter for relations.",
                },
                "include_archived": {
                    "type": "boolean",
                    "description": "Whether archived domains, records, and relations should be included.",
                    "default": False,
                },
                "include_records": {
                    "type": "boolean",
                    "description": "Include a bounded sample of records for the selected Domain.",
                    "default": False,
                },
                "include_relations": {
                    "type": "boolean",
                    "description": "Include a bounded sample of relations for the selected Domain.",
                    "default": False,
                },
                "include_events": {
                    "type": "boolean",
                    "description": "Include recent Domain change events for the selected Domain.",
                    "default": False,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum records, relations, or events to return, 1-100.",
                    "default": 25,
                },
            },
        ),
        "scope": external_agents.SCOPE_WORKSPACE_READ,
    },
    "illo_write_domain_record": {
        **_tool_schema(
            (
                "Create, update, or archive records in Illo Domains from a personal agent. "
                "Use this only when the user asks their personal agent to change a Domain "
                "record. This does not edit Domain schemas. Every write records DomainEvent "
                "trace metadata with the external-agent connection and token."
            ),
            {
                "action": {
                    "type": "string",
                    "description": "One of: create_record, update_record, archive_record.",
                },
                "domain_id": {"type": "integer", "description": "Optional Domain id."},
                "slug": {"type": "string", "description": "Optional Domain slug."},
                "object_key": {
                    "type": "string",
                    "description": "Required for create_record; identifies the Domain object type.",
                },
                "record_id": {
                    "type": "integer",
                    "description": "Required for update_record and archive_record.",
                },
                "data": {
                    "type": "object",
                    "description": "Record data for create_record.",
                    "default": {},
                },
                "data_patch": {
                    "type": "object",
                    "description": "Partial record data for update_record.",
                    "default": {},
                },
                "title": {
                    "type": "string",
                    "description": "Optional explicit record title.",
                },
                "expected_version": {
                    "type": "integer",
                    "description": "Optional optimistic concurrency version for update_record.",
                },
                "reason": {
                    "type": "string",
                    "description": "Optional human-readable reason to include in the Domain event trail.",
                },
            },
            ["action"],
        ),
        "scope": external_agents.SCOPE_DOMAIN_WRITE,
        "mutates_domain": True,
    },
}


def _clean_optional_string(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _clean_optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _clean_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _clean_string_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError("fields must be an array of strings")
    return list(value)


def _bounded_limit(value: Any, default: int = 25, maximum: int = 100) -> int:
    try:
        return max(1, min(int(value or default), maximum))
    except (TypeError, ValueError):
        return default


async def _resolve_domain(
    service: AsyncDomainService,
    *,
    org_id: str,
    domain_id: int | None,
    slug: str | None,
    include_archived: bool,
):
    if domain_id is not None:
        return await service.get_domain(org_id, domain_id, include_archived=include_archived)
    if slug:
        for domain in await service.list_domains(org_id, include_archived=include_archived):
            if str(domain.slug) == slug:
                return domain
        raise DomainNotFound(f"Domain slug '{slug}' not found")
    return None


async def _tool_inspect_domains(
    db: AsyncSession,
    principal: external_agents.AgentBridgePrincipal,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    service = AsyncDomainService(db)
    include_archived = bool(arguments.get("include_archived", False))
    limit = _bounded_limit(arguments.get("limit"))
    record_format = str(arguments.get("format") or "full").strip().lower()
    if record_format not in {"full", "compact"}:
        raise ValueError("format must be 'full' or 'compact'")
    record_order = str(arguments.get("order") or "updated_desc").strip().lower()
    if record_order not in {"updated_desc", "updated_asc"}:
        raise ValueError("order must be 'updated_desc' or 'updated_asc'")
    record_fields = _clean_string_list(arguments.get("fields"))
    record_filters = _clean_dict(arguments.get("filters"))
    domain = await _resolve_domain(
        service,
        org_id=principal.org_id,
        domain_id=_clean_optional_int(arguments.get("domain_id")),
        slug=_clean_optional_string(arguments.get("slug")),
        include_archived=include_archived,
    )
    if domain is None:
        return {
            "domains": [
                await service.serialize_domain_summary(item)
                for item in await service.list_domains(principal.org_id, include_archived=include_archived)
            ]
        }

    payload: dict[str, Any] = {"domain": await service.serialize_domain_schema(domain)}
    record_id = _clean_optional_int(arguments.get("record_id"))
    if record_id is not None:
        payload["record"] = await service.serialize_record(
            await service.get_record(principal.org_id, domain.id, record_id)
        )
    if bool(arguments.get("include_records", False)):
        object_key = _clean_optional_string(arguments.get("object_key"))
        search = _clean_optional_string(arguments.get("search"))
        record_rows = await service.list_records(
            principal.org_id,
            domain.id,
            object_key=object_key,
            search=search,
            filters=record_filters,
            include_archived=include_archived,
            limit=limit,
            order=record_order,
        )
        if record_format == "compact":
            records = [
                await service.serialize_record_compact(record, fields=record_fields)
                for record in record_rows
            ]
        else:
            records = [await service.serialize_record(record) for record in record_rows]
        total = await service.count_records(
            principal.org_id,
            domain.id,
            object_key=object_key,
            search=search,
            include_archived=include_archived,
            filters=record_filters,
        )
        payload.update(
            {
                "records": records,
                "returned": len(records),
                "total_matching": total,
                "order": record_order,
                "format": record_format,
            }
        )
    if bool(arguments.get("include_relations", False)):
        payload["relations"] = [
            await service.serialize_relation(relation)
            for relation in await service.list_relations(
                principal.org_id,
                domain.id,
                relation_key=_clean_optional_string(arguments.get("relation_key")),
                source_record_id=_clean_optional_int(arguments.get("source_record_id")),
                target_record_id=_clean_optional_int(arguments.get("target_record_id")),
                include_archived=include_archived,
                limit=limit,
            )
        ]
    if bool(arguments.get("include_events", False)):
        payload["events"] = [
            service.serialize_event(event)
            for event in await service.list_events(
                principal.org_id,
                domain.id,
                record_id=record_id,
                limit=limit,
            )
        ]
    return payload


def _domain_write_trace_reason(
    principal: external_agents.AgentBridgePrincipal,
    arguments: dict[str, Any],
) -> str:
    supplied = _clean_optional_string(arguments.get("reason"))
    trace = (
        f"illo_write_domain_record via {principal.connection_display_name} "
        f"({principal.agent_kind}); connection_id={principal.connection_id}; token_id={principal.token_id}"
    )
    return f"{supplied} | {trace}" if supplied else trace


async def _tool_write_domain_record(
    db: AsyncSession,
    principal: external_agents.AgentBridgePrincipal,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    action = str(arguments.get("action") or "").strip().lower()
    if action not in {"create_record", "update_record", "archive_record"}:
        raise ValueError("action must be one of: create_record, update_record, archive_record")
    service = AsyncDomainService(db)
    domain = await _resolve_domain(
        service,
        org_id=principal.org_id,
        domain_id=_clean_optional_int(arguments.get("domain_id")),
        slug=_clean_optional_string(arguments.get("slug")),
        include_archived=False,
    )
    if domain is None:
        raise ValueError("illo_write_domain_record requires domain_id or slug")

    actor_id = str(principal.owner_user_id)
    reason = _domain_write_trace_reason(principal, arguments)
    if action == "create_record":
        object_key = _clean_optional_string(arguments.get("object_key"))
        if not object_key:
            raise ValueError("create_record requires object_key")
        record = await service.create_record(
            principal.org_id,
            domain.id,
            object_key,
            data=_clean_dict(arguments.get("data")),
            title=_clean_optional_string(arguments.get("title")),
            actor_id=actor_id,
            actor_kind="personal_agent",
            reason=reason,
        )
        return {"record": await service.serialize_record(record)}

    record_id = _clean_optional_int(arguments.get("record_id"))
    if record_id is None:
        raise ValueError(f"{action} requires record_id")
    if action == "update_record":
        record = await service.update_record(
            principal.org_id,
            domain.id,
            record_id,
            data_patch=_clean_dict(arguments.get("data_patch")),
            title=_clean_optional_string(arguments.get("title")),
            expected_version=_clean_optional_int(arguments.get("expected_version")),
            actor_id=actor_id,
            actor_kind="personal_agent",
            reason=reason,
        )
        return {"record": await service.serialize_record(record)}

    result = await service.remove_record(
        principal.org_id,
        domain.id,
        record_id,
        mode="archive",
        actor_id=actor_id,
        actor_kind="personal_agent",
        reason=reason,
    )
    return result


DOMAIN_TOOL_HANDLERS: dict[str, ToolHandler] = {
    "illo_inspect_domains": _tool_inspect_domains,
    "illo_write_domain_record": _tool_write_domain_record,
}
