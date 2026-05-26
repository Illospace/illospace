"""Runtime binding broker for app-capsule data capabilities."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from brain.systems.user_domains.service import AsyncDomainService, DomainError, DomainNotFound
from brain.systems.workspace_apps.capabilities import (
    DOMAIN_BROKER_OPERATIONS,
    SYSTEM_READ_OPERATIONS,
    is_domain_write_operation,
)
from brain.systems.workspace_apps.service import WorkspaceAppError, a_active_version, a_get_app
from brain.systems.workspace_apps.system_bindings import query_system_binding_source


class WorkspaceAppBindingError(WorkspaceAppError):
    """Raised when an app runtime binding request is invalid."""


@dataclass(frozen=True)
class DomainBindingContext:
    session: AsyncSession
    org_id: str
    alias: str
    binding: Mapping[str, Any]
    operation: str
    payload: Mapping[str, Any]
    user_id: str | None
    service: AsyncDomainService
    domain_id: int
    object_key: str


DomainOperationHandler = Callable[[DomainBindingContext], Awaitable[Any]]


async def async_run_workspace_app_binding(
    session: AsyncSession,
    *,
    org_id: str,
    app_id: str,
    alias: str,
    operation: str,
    payload: Mapping[str, Any] | None = None,
    user_id: str | None = None,
    can_write: bool = False,
) -> dict[str, Any]:
    app = await a_get_app(session, org_id, app_id)
    version = await a_active_version(session, app.id)
    if version is None:
        raise WorkspaceAppBindingError("Workspace app has no active version")

    manifest = version.manifest if isinstance(version.manifest, Mapping) else {}
    binding = _resolve_binding(manifest, alias)
    op = str(operation or "").strip()
    if not op:
        raise WorkspaceAppBindingError("Binding operation is required")
    if op not in _operation_set(binding):
        raise WorkspaceAppBindingError(f"Binding '{alias}' does not allow operation '{op}'")

    kind = _binding_kind(manifest, binding)
    request_payload = dict(payload or {})
    if kind == "domain":
        data = await _run_domain_binding(
            session,
            org_id=org_id,
            alias=alias,
            binding=binding,
            operation=op,
            payload=request_payload,
            user_id=user_id,
            can_write=can_write,
        )
    elif kind == "system":
        data = await _run_system_binding(
            alias=alias,
            binding=binding,
            operation=op,
            payload=request_payload,
            org_id=org_id,
            user_id=user_id,
        )
    else:
        raise WorkspaceAppBindingError(f"Binding '{alias}' kind must be 'domain' or 'system'")

    return {
        "ok": True,
        "alias": alias,
        "operation": op,
        "kind": kind,
        "data": data,
        "warnings": [],
    }


def _resolve_binding(manifest: Mapping[str, Any], alias: str) -> Mapping[str, Any]:
    alias_text = str(alias or "").strip()
    if not alias_text:
        raise WorkspaceAppBindingError("Binding alias is required")
    data_plan = manifest.get("data_plan")
    if not isinstance(data_plan, Mapping):
        raise WorkspaceAppBindingError("Workspace app manifest has no data_plan")
    bindings = data_plan.get("bindings")
    if not isinstance(bindings, Mapping):
        raise WorkspaceAppBindingError("Workspace app manifest has no capability bindings")
    binding = bindings.get(alias_text)
    if not isinstance(binding, Mapping):
        raise WorkspaceAppBindingError(f"Workspace app binding '{alias_text}' is not declared")
    return binding


def _binding_kind(manifest: Mapping[str, Any], binding: Mapping[str, Any]) -> str:
    kind = str(binding.get("kind") or "").strip()
    if kind:
        return kind
    data_plan = manifest.get("data_plan")
    if isinstance(data_plan, Mapping) and data_plan.get("mode") == "domain":
        return "domain"
    if binding.get("source") or binding.get("source_key"):
        return "system"
    return "domain"


def _operation_set(binding: Mapping[str, Any]) -> set[str]:
    operations = binding.get("operations")
    if not isinstance(operations, list):
        return set()
    return {str(operation).strip() for operation in operations if str(operation).strip()}


async def _run_domain_binding(
    session: AsyncSession,
    *,
    org_id: str,
    alias: str,
    binding: Mapping[str, Any],
    operation: str,
    payload: Mapping[str, Any],
    user_id: str | None,
    can_write: bool,
) -> Any:
    if operation not in DOMAIN_BROKER_OPERATIONS:
        raise WorkspaceAppBindingError(f"Domain binding '{alias}' does not support operation '{operation}'")
    if is_domain_write_operation(operation) and not can_write:
        raise WorkspaceAppBindingError("Permission denied")

    domain_id = _positive_int(binding.get("domain_id"), f"binding '{alias}' domain_id")
    object_key = str(binding.get("object_key") or "").strip()
    if not object_key:
        raise WorkspaceAppBindingError(f"Domain binding '{alias}' requires object_key")

    service = AsyncDomainService(session)
    handler = _DOMAIN_OPERATION_HANDLERS.get(operation)
    if handler is None:
        raise WorkspaceAppBindingError(f"Domain operation '{operation}' is not implemented")
    context = DomainBindingContext(
        session=session,
        org_id=org_id,
        alias=alias,
        binding=binding,
        operation=operation,
        payload=payload,
        user_id=user_id,
        service=service,
        domain_id=domain_id,
        object_key=object_key,
    )
    try:
        return await handler(context)
    except (DomainError, DomainNotFound) as exc:
        raise WorkspaceAppBindingError(str(exc)) from exc


async def _domain_schema(context: DomainBindingContext) -> Any:
    return await context.service.serialize_domain_schema(
        await context.service.get_domain(context.org_id, context.domain_id)
    )


async def _domain_list(context: DomainBindingContext) -> Any:
    records = await _list_domain_records(context, default_limit=100)
    return [await context.service.serialize_record(record) for record in records]


async def _domain_aggregate(context: DomainBindingContext) -> Any:
    records = [
        await context.service.serialize_record(record)
        for record in await _list_domain_records(context, default_limit=500)
    ]
    return _aggregate_records(records, context.payload)


async def _domain_get(context: DomainBindingContext) -> Any:
    return await context.service.serialize_record(
        await context.service.get_record(context.org_id, context.domain_id, _record_id(context.payload))
    )


async def _domain_create(context: DomainBindingContext) -> Any:
    record = await context.service.create_record(
        context.org_id,
        context.domain_id,
        context.object_key,
        data=_record_data(context.payload),
        title=_optional_text(context.payload.get("title")),
        actor_id=context.user_id,
        actor_kind="human",
    )
    return await context.service.serialize_record(record)


async def _domain_update(context: DomainBindingContext) -> Any:
    record = await context.service.update_record(
        context.org_id,
        context.domain_id,
        _record_id(context.payload),
        data_patch=_record_patch(context.payload),
        title=_optional_text(context.payload.get("title")),
        expected_version=_optional_int(context.payload.get("expectedVersion") or context.payload.get("expected_version")),
        actor_id=context.user_id,
        actor_kind="human",
    )
    return await context.service.serialize_record(record)


async def _domain_archive(context: DomainBindingContext) -> Any:
    return await context.service.remove_record(
        context.org_id,
        context.domain_id,
        _record_id(context.payload),
        mode="archive",
        actor_id=context.user_id,
        actor_kind="human",
    )


async def _domain_bulk_update(context: DomainBindingContext) -> Any:
    updates = context.payload.get("updates")
    if not isinstance(updates, list):
        raise WorkspaceAppBindingError("bulkUpdate requires updates")
    results = []
    for update in updates:
        if not isinstance(update, Mapping):
            raise WorkspaceAppBindingError("bulkUpdate updates must be objects")
        record = await context.service.update_record(
            context.org_id,
            context.domain_id,
            _record_id(update),
            data_patch=_record_patch(update),
            title=_optional_text(update.get("title")),
            expected_version=_optional_int(update.get("expectedVersion") or update.get("expected_version")),
            actor_id=context.user_id,
            actor_kind="human",
        )
        results.append(await context.service.serialize_record(record))
    return results


async def _list_domain_records(context: DomainBindingContext, *, default_limit: int) -> list[Any]:
    return await context.service.list_records(
        context.org_id,
        context.domain_id,
        object_key=context.object_key,
        search=_optional_text(context.payload.get("search") or context.payload.get("query")),
        include_archived=bool(context.payload.get("includeArchived") or context.payload.get("include_archived") or False),
        limit=_limit(context.payload.get("limit"), default=default_limit, maximum=500),
    )


_DOMAIN_OPERATION_HANDLERS: dict[str, DomainOperationHandler] = {
    "schema": _domain_schema,
    "list": _domain_list,
    "query": _domain_list,
    "aggregate": _domain_aggregate,
    "get": _domain_get,
    "create": _domain_create,
    "update": _domain_update,
    "archive": _domain_archive,
    "bulkUpdate": _domain_bulk_update,
}


async def _run_system_binding(
    *,
    alias: str,
    binding: Mapping[str, Any],
    operation: str,
    payload: Mapping[str, Any],
    org_id: str,
    user_id: str | None,
) -> Any:
    if operation not in SYSTEM_READ_OPERATIONS:
        raise WorkspaceAppBindingError(f"System binding '{alias}' only supports read operations")
    source = str(binding.get("source") or binding.get("source_key") or "").strip()
    if not source:
        raise WorkspaceAppBindingError(f"System binding '{alias}' requires source")
    if operation == "schema":
        return {
            "alias": alias,
            "kind": "system",
            "source": source,
            "operations": sorted(SYSTEM_READ_OPERATIONS),
        }

    result = await query_system_binding_source(
        source=source,
        query=_optional_text(payload.get("query")),
        search=_optional_text(payload.get("search")),
        time_window=str(payload.get("timeWindow") or payload.get("time_window") or "last_30d"),
        start_at=_optional_text(payload.get("startAt") or payload.get("start_at")),
        end_at=_optional_text(payload.get("endAt") or payload.get("end_at")),
        limit=_limit(payload.get("limit"), default=50, maximum=100),
        idea_id=_optional_text(payload.get("ideaId") or payload.get("idea_id")),
        domain_id=_optional_int(payload.get("domainId") or payload.get("domain_id")),
        object_key=_optional_text(payload.get("objectKey") or payload.get("object_key")),
        include_archived=bool(payload.get("includeArchived") or payload.get("include_archived") or False),
        user_id=user_id,
        org_id=org_id,
    )
    if operation == "aggregate":
        return {"counts": result.get("counts", {}), "total_count": result.get("total_count", 0)}
    return result


def _record_id(payload: Mapping[str, Any]) -> int:
    value = payload.get("recordId", payload.get("record_id", payload.get("id")))
    return _positive_int(value, "recordId")


def _record_data(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = payload.get("data")
    if value is None:
        value = payload.get("record")
    if not isinstance(value, Mapping):
        raise WorkspaceAppBindingError("create requires data")
    return dict(value)


def _record_patch(payload: Mapping[str, Any]) -> dict[str, Any]:
    value = payload.get("dataPatch", payload.get("data_patch", payload.get("patch")))
    if value is None:
        value = payload.get("data")
    if not isinstance(value, Mapping):
        raise WorkspaceAppBindingError("update requires dataPatch")
    return dict(value)


def _aggregate_records(records: list[dict[str, Any]], payload: Mapping[str, Any]) -> dict[str, Any]:
    group_by = _optional_text(payload.get("groupBy") or payload.get("group_by"))
    if not group_by:
        return {"total": len(records), "groups": []}

    groups: dict[str, dict[str, Any]] = {}
    for record in records:
        value = _record_value(record, group_by)
        key = str(value if value not in (None, "") else "(empty)")
        group = groups.setdefault(key, {"key": value, "label": key, "count": 0})
        group["count"] += 1
    return {"total": len(records), "groupBy": group_by, "groups": list(groups.values())}


def _record_value(record: Mapping[str, Any], key: str) -> Any:
    if key in record:
        return record.get(key)
    data = record.get("data")
    if isinstance(data, Mapping):
        return data.get(key)
    return None


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise WorkspaceAppBindingError("Expected numeric value") from exc


def _positive_int(value: Any, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise WorkspaceAppBindingError(f"{field} must be numeric") from exc
    if parsed <= 0:
        raise WorkspaceAppBindingError(f"{field} must be positive")
    return parsed


def _limit(value: Any, *, default: int, maximum: int) -> int:
    if value in (None, ""):
        return default
    try:
        return max(1, min(int(value), maximum))
    except (TypeError, ValueError):
        return default
