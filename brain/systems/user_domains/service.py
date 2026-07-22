"""Domain validation, persistence, and serialization helpers."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timezone
import json
import re
from typing import Any, Iterable, Sequence
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.domain import (
    Domain,
    DomainEvent,
    DomainFieldDefinition,
    DomainObjectType,
    DomainRecord,
    DomainRelation,
    DomainRelationType,
)

KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,78}[a-z0-9])?$")
SUPPORTED_FIELD_TYPES = frozenset(
    {
        "text",
        "long_text",
        "number",
        "boolean",
        "date",
        "datetime",
        "enum",
        "multi_enum",
        "url",
        "email",
        "phone",
        "money",
        "user_ref",
        "record_ref",
        "json",
    }
)
SUPPORTED_CARDINALITIES = frozenset(
    {"one_to_one", "one_to_many", "many_to_one", "many_to_many"}
)


class DomainError(ValueError):
    """Base exception for user-facing domain validation errors."""


class DomainFieldTypeError(DomainError):
    """Typed validation error for an unsupported Domain field type."""

    code = "invalid_field_type"

    def __init__(self, field_type: Any):
        self.field_type = str(field_type or "").strip().lower()
        self.allowed_values = tuple(sorted(SUPPORTED_FIELD_TYPES))
        super().__init__(
            f"Unsupported field_type {self.field_type!r}; use one of: "
            f"{', '.join(self.allowed_values)}"
        )


class DomainNotFound(LookupError):
    """Raised when a requested domain row is not visible in the caller org."""


def _validate_record_data(
    fields: Iterable[DomainFieldDefinition],
    data: dict[str, Any],
) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise DomainError("record data must be an object")
    field_map = {field.key: field for field in fields}
    unknown = sorted(set(data) - set(field_map))
    if unknown:
        raise DomainError(f"Unknown field(s): {', '.join(unknown)}")

    normalized: dict[str, Any] = {}
    for key, field in field_map.items():
        value = data.get(key, field.default_value)
        if _is_empty(value):
            if field.required:
                raise DomainError(f"Field '{key}' is required")
            normalized[key] = None
            continue
        normalized_value = _coerce_field_value(field, value)
        _validate_field_constraints(field, normalized_value)
        normalized[key] = normalized_value
    return normalized


def _serialize_field(field: DomainFieldDefinition) -> dict[str, Any]:
    return {
        "id": field.id,
        "domain_id": field.domain_id,
        "object_type_id": field.object_type_id,
        "key": field.key,
        "name": field.name,
        "field_type": field.field_type,
        "required": field.required,
        "options": field.options or [],
        "default_value": field.default_value,
        "validation": field.validation or {},
        "searchable": field.searchable,
        "sortable": field.sortable,
        "created_at": field.created_at,
        "updated_at": field.updated_at,
    }


def _serialize_event(event: DomainEvent) -> dict[str, Any]:
    return {
        "id": event.id,
        "org_id": event.org_id,
        "domain_id": event.domain_id,
        "record_id": event.record_id,
        "relation_id": event.relation_id,
        "event_type": event.event_type,
        "actor_kind": event.actor_kind,
        "actor_id": event.actor_id,
        "run_id": event.run_id,
        "idea_id": event.idea_id,
        "before": event.before or {},
        "after": event.after or {},
        "patch": event.patch or {},
        "reason": event.reason,
        "created_at": event.created_at,
    }


def _uuid_or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return str(UUID(text))
    except (TypeError, ValueError, AttributeError):
        return None


def _record_filter_value(record: DomainRecord, key: str) -> Any:
    clean_key = str(key or "").strip()
    if clean_key in {"id", "record_id"}:
        return record.id
    if clean_key == "title":
        return record.title
    if clean_key == "version":
        return record.version
    if clean_key == "object_type_id":
        return record.object_type_id
    data = record.data if isinstance(record.data, dict) else {}
    path = clean_key[5:] if clean_key.startswith("data.") else clean_key
    current: Any = data
    for part in [item for item in path.split(".") if item]:
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def _values_equal(left: Any, right: Any) -> bool:
    if isinstance(left, str) or isinstance(right, str):
        return str(left or "").strip().lower() == str(right or "").strip().lower()
    return left == right


def _value_in(actual: Any, choices: Any) -> bool:
    if not isinstance(choices, list | tuple | set):
        choices = [choices]
    if isinstance(actual, list | tuple | set):
        return any(_values_equal(item, choice) for item in actual for choice in choices)
    return any(_values_equal(actual, choice) for choice in choices)


def _value_contains(actual: Any, expected: Any) -> bool:
    if isinstance(actual, str):
        return str(expected or "").strip().lower() in actual.strip().lower()
    if isinstance(actual, list | tuple | set):
        return any(_values_equal(item, expected) for item in actual)
    if isinstance(actual, Mapping):
        return str(expected) in actual
    return _values_equal(actual, expected)


def _matches_record_filter(actual: Any, expected: Any) -> bool:
    if isinstance(expected, Mapping):
        if "exists" in expected and bool(expected.get("exists")) != (actual is not None):
            return False
        if "eq" in expected and not _values_equal(actual, expected.get("eq")):
            return False
        choices = expected.get("in", expected.get("any_of"))
        if choices is not None and not _value_in(actual, choices):
            return False
        if "contains" in expected and not _value_contains(actual, expected.get("contains")):
            return False
        return True
    if isinstance(expected, list | tuple | set):
        return _value_in(actual, expected)
    if isinstance(actual, list | tuple | set):
        return _value_in(actual, [expected])
    return _values_equal(actual, expected)


def _record_matches_filters(record: DomainRecord, filters: Mapping[str, Any] | None) -> bool:
    if not filters:
        return True
    for key, expected in filters.items():
        if not _matches_record_filter(_record_filter_value(record, str(key)), expected):
            return False
    return True


class AsyncDomainService:
    """AsyncSession-backed domain service for request/runtime code."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def list_domains(
        self,
        org_id: str,
        *,
        include_archived: bool = False,
    ) -> Sequence[Domain]:
        stmt = select(Domain).where(Domain.org_id == org_id)
        if not include_archived:
            stmt = stmt.where(Domain.archived_at.is_(None))
        stmt = stmt.order_by(Domain.updated_at.desc(), Domain.id.desc())
        return (await self.session.scalars(stmt)).all()

    async def get_domain(
        self,
        org_id: str,
        domain_id: int,
        *,
        include_archived: bool = False,
    ) -> Domain:
        stmt = select(Domain).where(Domain.id == domain_id, Domain.org_id == org_id)
        if not include_archived:
            stmt = stmt.where(Domain.archived_at.is_(None))
        domain = (await self.session.scalars(stmt)).first()
        if domain is None:
            raise DomainNotFound(f"Domain {domain_id} not found")
        return domain

    async def create_domain(
        self,
        org_id: str,
        *,
        name: str,
        slug: str | None = None,
        description: str | None = None,
        objects: Sequence[dict[str, Any]] | None = None,
        relations: Sequence[dict[str, Any]] | None = None,
        actor_id: str | None = None,
        actor_kind: str = "human",
    ) -> Domain:
        name = _nonempty(name, "name")
        slug = _normalize_slug(slug or name)
        await self._ensure_slug_available(org_id, slug)
        domain = Domain(
            org_id=org_id,
            slug=slug,
            name=name,
            description=_clean_optional(description),
            created_by_user_id=actor_id,
            updated_by_user_id=actor_id,
        )
        self.session.add(domain)
        await self.session.flush()

        object_map: dict[str, DomainObjectType] = {}
        for idx, payload in enumerate(objects or []):
            obj = await self.add_object_type(
                domain,
                payload,
                actor_id=actor_id,
                actor_kind=actor_kind,
                emit_event=False,
                sort_order=idx,
            )
            object_map[obj.key] = obj

        for payload in relations or []:
            await self.add_relation_type(
                domain,
                payload,
                object_map=object_map,
                actor_id=actor_id,
                actor_kind=actor_kind,
                emit_event=False,
            )

        await self._add_event(
            org_id=org_id,
            domain_id=domain.id,
            event_type="domain.created",
            actor_id=actor_id,
            actor_kind=actor_kind,
            after=await self.serialize_domain_schema(domain),
        )
        return domain

    async def remove_domain(
        self,
        org_id: str,
        domain_id: int,
        *,
        mode: str = "archive",
        actor_id: str | None = None,
        actor_kind: str = "human",
        run_id: int | None = None,
        idea_id: str | None = None,
    ) -> dict[str, Any]:
        domain = await self.get_domain(org_id, domain_id)
        before = await self.serialize_domain_schema(domain)
        if mode == "archive":
            domain.archived_at = datetime.now(timezone.utc)
            domain.updated_by_user_id = actor_id
            await self.session.flush()
            await self.session.refresh(domain)
            await self._add_event(
                org_id=org_id,
                domain_id=domain.id,
                event_type="domain.archived",
                actor_id=actor_id,
                actor_kind=actor_kind,
                run_id=run_id,
                idea_id=idea_id,
                before=before,
                after=await self.serialize_domain_schema(domain),
            )
            return {"id": domain_id, "mode": "archive", "archived": True}
        if mode == "delete":
            await self.session.delete(domain)
            await self.session.flush()
            return {"id": domain_id, "mode": "delete", "deleted": True}
        raise DomainError("mode must be 'archive' or 'delete'")

    async def add_object_type(
        self,
        domain: Domain,
        payload: dict[str, Any],
        *,
        actor_id: str | None = None,
        actor_kind: str = "human",
        emit_event: bool = True,
        sort_order: int | None = None,
    ) -> DomainObjectType:
        key = _normalize_key(payload.get("key") or payload.get("name"), "object key")
        await self._ensure_object_key_available(domain.id, key)
        obj = DomainObjectType(
            domain_id=domain.id,
            key=key,
            name=_nonempty(payload.get("name") or _title_from_key(key), "object name"),
            description=_clean_optional(payload.get("description")),
            title_field=_clean_optional(payload.get("title_field")),
            sort_order=sort_order if sort_order is not None else int(payload.get("sort_order") or 0),
        )
        self.session.add(obj)
        await self.session.flush()
        for field in payload.get("fields") or []:
            await self.add_field_definition(obj, field, emit_event=False)
        if emit_event:
            domain.updated_by_user_id = actor_id
            await self._add_event(
                org_id=domain.org_id,
                domain_id=domain.id,
                event_type="schema.object_added",
                actor_id=actor_id,
                actor_kind=actor_kind,
                after=await self.serialize_object_type(obj),
            )
        return obj

    async def add_field_definition(
        self,
        object_type: DomainObjectType,
        payload: dict[str, Any],
        *,
        emit_event: bool = True,
    ) -> DomainFieldDefinition:
        key = _normalize_key(payload.get("key") or payload.get("name"), "field key")
        await self._ensure_field_key_available(object_type.id, key)
        field_type = _normalize_field_type(payload.get("field_type") or payload.get("type"))
        options = payload.get("options") or []
        if field_type in {"enum", "multi_enum"} and not options:
            raise DomainError(f"Field '{key}' requires non-empty options")
        if not isinstance(options, list):
            raise DomainError(f"Field '{key}' options must be a list")
        validation = payload.get("validation") or {}
        if not isinstance(validation, Mapping):
            raise DomainError(f"Field '{key}' validation must be an object")
        field = DomainFieldDefinition(
            domain_id=object_type.domain_id,
            object_type_id=object_type.id,
            key=key,
            name=_nonempty(payload.get("name") or _title_from_key(key), "field name"),
            field_type=field_type,
            required=bool(payload.get("required", False)),
            options=[str(option) for option in options],
            default_value=payload.get("default_value"),
            validation=dict(validation),
            searchable=bool(payload.get("searchable", True)),
            sortable=bool(payload.get("sortable", True)),
        )
        self.session.add(field)
        await self.session.flush()
        if emit_event:
            await self._add_event(
                org_id=await self._domain_org_id(object_type.domain_id),
                domain_id=object_type.domain_id,
                event_type="schema.field_added",
                after=self.serialize_field(field),
            )
        return field

    async def add_relation_type(
        self,
        domain: Domain,
        payload: dict[str, Any],
        *,
        object_map: dict[str, DomainObjectType] | None = None,
        actor_id: str | None = None,
        actor_kind: str = "human",
        emit_event: bool = True,
    ) -> DomainRelationType:
        key = _normalize_key(payload.get("key") or payload.get("name"), "relation key")
        await self._ensure_relation_type_key_available(domain.id, key)
        object_map = object_map or await self._object_map(domain.id)
        source_key = _normalize_key(payload.get("source_object") or payload.get("source"), "source object")
        target_key = _normalize_key(payload.get("target_object") or payload.get("target"), "target object")
        source = object_map.get(source_key)
        target = object_map.get(target_key)
        if not source or not target:
            raise DomainError("Relation source_object and target_object must exist in the domain")
        cardinality = str(payload.get("cardinality") or "many_to_many").strip()
        if cardinality not in SUPPORTED_CARDINALITIES:
            raise DomainError(
                f"cardinality must be one of: {', '.join(sorted(SUPPORTED_CARDINALITIES))}"
            )
        relation_type = DomainRelationType(
            domain_id=domain.id,
            key=key,
            name=_nonempty(payload.get("name") or _title_from_key(key), "relation name"),
            description=_clean_optional(payload.get("description")),
            source_object_type_id=source.id,
            target_object_type_id=target.id,
            cardinality=cardinality,
        )
        self.session.add(relation_type)
        await self.session.flush()
        if emit_event:
            domain.updated_by_user_id = actor_id
            await self._add_event(
                org_id=domain.org_id,
                domain_id=domain.id,
                event_type="schema.relation_type_added",
                actor_id=actor_id,
                actor_kind=actor_kind,
                after=await self.serialize_relation_type(relation_type),
            )
        return relation_type

    async def list_records(
        self,
        org_id: str,
        domain_id: int,
        *,
        object_key: str | None = None,
        search: str | None = None,
        filters: Mapping[str, Any] | None = None,
        include_archived: bool = False,
        limit: int = 100,
        order: str = "updated_desc",
        offset: int = 0,
    ) -> Sequence[DomainRecord]:
        if order not in {"updated_desc", "updated_asc"}:
            raise DomainError("order must be 'updated_desc' or 'updated_asc'")
        where_clauses = await self._record_where_clauses(
            org_id,
            domain_id,
            object_key=object_key,
            search=search,
            include_archived=include_archived,
        )
        stmt = select(DomainRecord).where(*where_clauses)
        max_results = max(1, min(int(limit), 500))
        page_offset = max(0, int(offset or 0))
        if order == "updated_asc":
            stmt = stmt.order_by(DomainRecord.updated_at.asc(), DomainRecord.id.asc())
        else:
            stmt = stmt.order_by(DomainRecord.updated_at.desc(), DomainRecord.id.desc())
        if filters:
            stmt = stmt.limit(500)
        else:
            stmt = stmt.offset(page_offset).limit(max_results)
        records = (await self.session.scalars(stmt)).all()
        if filters:
            matches = [record for record in records if _record_matches_filters(record, filters)]
            return matches[page_offset : page_offset + max_results]
        return records

    async def count_records(
        self,
        org_id: str,
        domain_id: int,
        *,
        object_key: str | None = None,
        search: str | None = None,
        include_archived: bool = False,
        filters: Mapping[str, Any] | None = None,
    ) -> int:
        where_clauses = await self._record_where_clauses(
            org_id,
            domain_id,
            object_key=object_key,
            search=search,
            include_archived=include_archived,
        )
        if filters:
            records = (
                await self.session.scalars(select(DomainRecord).where(*where_clauses))
            ).all()
            return sum(_record_matches_filters(record, filters) for record in records)
        stmt = select(func.count()).select_from(DomainRecord).where(*where_clauses)
        return int(await self.session.scalar(stmt) or 0)

    async def _record_where_clauses(
        self,
        org_id: str,
        domain_id: int,
        *,
        object_key: str | None,
        search: str | None,
        include_archived: bool,
    ) -> list[Any]:
        domain = await self.get_domain(org_id, domain_id)
        where_clauses = [
            DomainRecord.org_id == org_id,
            DomainRecord.domain_id == domain.id,
        ]
        if object_key:
            obj = await self.get_object_type(domain.id, object_key)
            where_clauses.append(DomainRecord.object_type_id == obj.id)
        if not include_archived:
            where_clauses.append(DomainRecord.archived_at.is_(None))
        if search and search.strip():
            where_clauses.append(DomainRecord.search_text.ilike(f"%{search.strip()}%"))
        return where_clauses

    async def get_record(self, org_id: str, domain_id: int, record_id: int) -> DomainRecord:
        await self.get_domain(org_id, domain_id)
        stmt = select(DomainRecord).where(
            DomainRecord.id == record_id,
            DomainRecord.org_id == org_id,
            DomainRecord.domain_id == domain_id,
        )
        record = (await self.session.scalars(stmt)).first()
        if record is None:
            raise DomainNotFound(f"Record {record_id} not found")
        return record

    async def create_record(
        self,
        org_id: str,
        domain_id: int,
        object_key: str,
        *,
        data: dict[str, Any],
        title: str | None = None,
        actor_id: str | None = None,
        actor_kind: str = "human",
        run_id: int | None = None,
        idea_id: str | None = None,
        reason: str | None = None,
    ) -> DomainRecord:
        domain = await self.get_domain(org_id, domain_id)
        # Serialize create/upsert decisions for this object type. Without this
        # lock, two concurrent observations can both pass the application-level
        # uniqueness check before either inserts its PR record.
        obj = await self.get_object_type(domain.id, object_key, for_update=True)
        fields = await self.list_fields(obj.id)
        natural_key = _tracker_pr_natural_key(fields, data)
        if natural_key is not None:
            existing = await self._find_active_tracker_pr_records(
                org_id,
                domain.id,
                obj.id,
                fields,
                natural_key,
            )
            if existing:
                return await self._merge_tracker_pr_records(
                    org_id,
                    domain.id,
                    existing,
                    data=data,
                    title=title,
                    actor_id=actor_id,
                    actor_kind=actor_kind,
                    run_id=run_id,
                    idea_id=idea_id,
                    reason=reason,
                )

        normalized = self.validate_record_data(fields, data)
        if domain.slug == "github-ticket-tracker" and obj.key == "chantier":
            # The tracker schema predates object-level constraints. Keep this
            # cross-field invariant at the shared persistence boundary so
            # declare, completion repair, and manage_domain cannot create the
            # vaporware-shaped placeholder from issue #386.
            from brain.systems.chantiers import is_placeholder_chantier

            if is_placeholder_chantier(normalized):
                raise DomainError(
                    "Chantier placeholder records are rejected: provide at least one durable "
                    "ref, an explicit Done-means goal or next step, or an owner"
                )
        record = DomainRecord(
            org_id=org_id,
            domain_id=domain.id,
            object_type_id=obj.id,
            title=title.strip() if title and title.strip() else "Untitled",
            data=normalized,
            search_text="",
            created_by_user_id=actor_id,
            updated_by_user_id=actor_id,
        )
        self.session.add(record)
        await self.session.flush()
        record.title = _record_title(obj, fields, normalized, title=title, record_id=record.id)
        record.search_text = _record_search_text(record.title, fields, normalized)
        domain.updated_by_user_id = actor_id
        await self._add_event(
            org_id=org_id,
            domain_id=domain.id,
            record_id=record.id,
            event_type="record.created",
            actor_id=actor_id,
            actor_kind=actor_kind,
            run_id=run_id,
            idea_id=idea_id,
            after=await self.serialize_record(record),
            reason=reason,
        )
        await self.session.refresh(record)
        return record

    async def _find_active_tracker_pr_records(
        self,
        org_id: str,
        domain_id: int,
        object_type_id: int,
        fields: Sequence[DomainFieldDefinition],
        natural_key: tuple[str, str],
    ) -> list[DomainRecord]:
        stmt = (
            select(DomainRecord)
            .where(
                DomainRecord.org_id == org_id,
                DomainRecord.domain_id == domain_id,
                DomainRecord.object_type_id == object_type_id,
                DomainRecord.archived_at.is_(None),
            )
            .order_by(DomainRecord.id)
        )
        records = (await self.session.scalars(stmt)).all()
        return [
            record
            for record in records
            if _tracker_pr_natural_key(fields, record.data or {}) == natural_key
        ]

    async def _merge_tracker_pr_records(
        self,
        org_id: str,
        domain_id: int,
        records: Sequence[DomainRecord],
        *,
        data: dict[str, Any],
        title: str | None,
        actor_id: str | None,
        actor_kind: str,
        run_id: int | None,
        idea_id: str | None,
        reason: str | None,
    ) -> DomainRecord:
        # Domain record ids are monotonically assigned, so the lowest id is the
        # deterministic proxy for the earliest-created canonical row.
        canonical, *duplicates = sorted(records, key=lambda record: record.id)
        merged_data = dict(canonical.data or {})
        for duplicate in duplicates:
            for key, value in (duplicate.data or {}).items():
                if _is_empty(merged_data.get(key)) and not _is_empty(value):
                    merged_data[key] = value
        # The current observation is authoritative for every field it supplies.
        merged_data.update(data)

        canonical = await self.update_record(
            org_id,
            domain_id,
            canonical.id,
            data_patch=merged_data,
            title=title,
            actor_id=actor_id,
            actor_kind=actor_kind,
            run_id=run_id,
            idea_id=idea_id,
            reason=reason,
        )
        for duplicate in duplicates:
            merge_reason = f"Merged duplicate PR record into record {canonical.id}"
            if reason:
                merge_reason = f"{reason} | {merge_reason}"
            await self.remove_record(
                org_id,
                domain_id,
                duplicate.id,
                mode="archive",
                actor_id=actor_id,
                actor_kind=actor_kind,
                run_id=run_id,
                idea_id=idea_id,
                reason=merge_reason,
            )
        return canonical

    async def update_record(
        self,
        org_id: str,
        domain_id: int,
        record_id: int,
        *,
        data_patch: dict[str, Any],
        title: str | None = None,
        expected_version: int | None = None,
        actor_id: str | None = None,
        actor_kind: str = "human",
        run_id: int | None = None,
        idea_id: str | None = None,
        reason: str | None = None,
    ) -> DomainRecord:
        record = await self.get_record(org_id, domain_id, record_id)
        if expected_version is not None and record.version != expected_version:
            raise DomainError(
                f"Record version mismatch: expected {expected_version}, current {record.version}"
            )
        obj = await self.get_object_type_by_id(record.object_type_id)
        fields = await self.list_fields(obj.id)
        before = await self.serialize_record(record)
        _validate_immutable_field_updates(fields, record.data or {}, data_patch or {})
        merged = dict(record.data or {})
        merged.update(data_patch or {})
        normalized = self.validate_record_data(fields, merged)
        record.data = normalized
        record.version += 1
        record.updated_by_user_id = actor_id
        if title is not None and title.strip():
            record.title = title.strip()
        else:
            record.title = _record_title(obj, fields, normalized, record_id=record.id)
        record.search_text = _record_search_text(record.title, fields, normalized)
        domain = await self.get_domain(org_id, domain_id)
        domain.updated_by_user_id = actor_id
        await self.session.flush()
        await self.session.refresh(record)
        await self._add_event(
            org_id=org_id,
            domain_id=domain.id,
            record_id=record.id,
            event_type="record.updated",
            actor_id=actor_id,
            actor_kind=actor_kind,
            run_id=run_id,
            idea_id=idea_id,
            before=before,
            after=await self.serialize_record(record),
            patch=data_patch,
            reason=reason,
        )
        await self.session.refresh(record)
        return record

    async def remove_record(
        self,
        org_id: str,
        domain_id: int,
        record_id: int,
        *,
        mode: str = "archive",
        actor_id: str | None = None,
        actor_kind: str = "human",
        run_id: int | None = None,
        idea_id: str | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        record = await self.get_record(org_id, domain_id, record_id)
        before = await self.serialize_record(record)
        if mode == "archive":
            record.archived_at = datetime.now(timezone.utc)
            record.version += 1
            record.updated_by_user_id = actor_id
            await self.session.flush()
            await self.session.refresh(record)
            await self._add_event(
                org_id=org_id,
                domain_id=domain_id,
                record_id=record.id,
                event_type="record.archived",
                actor_id=actor_id,
                actor_kind=actor_kind,
                run_id=run_id,
                idea_id=idea_id,
                before=before,
                after=await self.serialize_record(record),
                reason=reason,
            )
            return {"id": record_id, "mode": "archive", "archived": True}
        if mode == "delete":
            await self._add_event(
                org_id=org_id,
                domain_id=domain_id,
                record_id=record.id,
                event_type="record.deleted",
                actor_id=actor_id,
                actor_kind=actor_kind,
                run_id=run_id,
                idea_id=idea_id,
                before=before,
                reason=reason,
            )
            await self.session.delete(record)
            await self.session.flush()
            return {"id": record_id, "mode": "delete", "deleted": True}
        raise DomainError("mode must be 'archive' or 'delete'")

    async def create_relation(
        self,
        org_id: str,
        domain_id: int,
        relation_key: str,
        *,
        source_record_id: int,
        target_record_id: int,
        properties: dict[str, Any] | None = None,
        actor_id: str | None = None,
        actor_kind: str = "human",
        run_id: int | None = None,
        idea_id: str | None = None,
    ) -> DomainRelation:
        domain = await self.get_domain(org_id, domain_id)
        relation_type = await self.get_relation_type(domain.id, relation_key)
        source = await self.get_record(org_id, domain.id, source_record_id)
        target = await self.get_record(org_id, domain.id, target_record_id)
        if source.object_type_id != relation_type.source_object_type_id:
            raise DomainError("source_record_id does not match relation source object type")
        if target.object_type_id != relation_type.target_object_type_id:
            raise DomainError("target_record_id does not match relation target object type")
        relation = DomainRelation(
            org_id=org_id,
            domain_id=domain.id,
            relation_type_id=relation_type.id,
            source_record_id=source.id,
            target_record_id=target.id,
            properties=properties or {},
            created_by_user_id=actor_id,
        )
        self.session.add(relation)
        await self.session.flush()
        await self._add_event(
            org_id=org_id,
            domain_id=domain.id,
            relation_id=relation.id,
            event_type="relation.created",
            actor_id=actor_id,
            actor_kind=actor_kind,
            run_id=run_id,
            idea_id=idea_id,
            after=await self.serialize_relation(relation),
        )
        return relation

    async def list_relations(
        self,
        org_id: str,
        domain_id: int,
        *,
        relation_key: str | None = None,
        source_record_id: int | None = None,
        target_record_id: int | None = None,
        include_archived: bool = False,
        limit: int = 100,
    ) -> Sequence[DomainRelation]:
        domain = await self.get_domain(org_id, domain_id)
        stmt = select(DomainRelation).where(
            DomainRelation.org_id == org_id,
            DomainRelation.domain_id == domain.id,
        )
        if relation_key:
            relation_type = await self.get_relation_type(domain.id, relation_key)
            stmt = stmt.where(DomainRelation.relation_type_id == relation_type.id)
        if source_record_id is not None:
            stmt = stmt.where(DomainRelation.source_record_id == source_record_id)
        if target_record_id is not None:
            stmt = stmt.where(DomainRelation.target_record_id == target_record_id)
        if not include_archived:
            stmt = stmt.where(DomainRelation.archived_at.is_(None))
        stmt = stmt.order_by(DomainRelation.updated_at.desc(), DomainRelation.id.desc()).limit(
            max(1, min(int(limit), 500))
        )
        return (await self.session.scalars(stmt)).all()

    async def remove_relation(
        self,
        org_id: str,
        domain_id: int,
        relation_id: int,
        *,
        mode: str = "archive",
        actor_id: str | None = None,
        actor_kind: str = "human",
        run_id: int | None = None,
        idea_id: str | None = None,
    ) -> dict[str, Any]:
        domain = await self.get_domain(org_id, domain_id)
        stmt = select(DomainRelation).where(
            DomainRelation.id == relation_id,
            DomainRelation.org_id == org_id,
            DomainRelation.domain_id == domain_id,
        )
        relation = (await self.session.scalars(stmt)).first()
        if relation is None:
            raise DomainNotFound(f"Relation {relation_id} not found")
        before = await self.serialize_relation(relation)
        if mode == "archive":
            relation.archived_at = datetime.now(timezone.utc)
            domain.updated_by_user_id = actor_id
            await self.session.flush()
            await self.session.refresh(relation)
            await self._add_event(
                org_id=org_id,
                domain_id=domain_id,
                relation_id=relation.id,
                event_type="relation.archived",
                actor_id=actor_id,
                actor_kind=actor_kind,
                run_id=run_id,
                idea_id=idea_id,
                before=before,
                after=await self.serialize_relation(relation),
            )
            return {"id": relation_id, "mode": "archive", "archived": True}
        if mode == "delete":
            domain.updated_by_user_id = actor_id
            await self._add_event(
                org_id=org_id,
                domain_id=domain_id,
                relation_id=relation.id,
                event_type="relation.deleted",
                actor_id=actor_id,
                actor_kind=actor_kind,
                run_id=run_id,
                idea_id=idea_id,
                before=before,
            )
            await self.session.delete(relation)
            await self.session.flush()
            return {"id": relation_id, "mode": "delete", "deleted": True}
        raise DomainError("mode must be 'archive' or 'delete'")

    async def list_events(
        self,
        org_id: str,
        domain_id: int,
        *,
        record_id: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[DomainEvent]:
        await self.get_domain(org_id, domain_id)
        stmt = select(DomainEvent).where(
            DomainEvent.org_id == org_id,
            DomainEvent.domain_id == domain_id,
        )
        if record_id is not None:
            stmt = stmt.where(DomainEvent.record_id == record_id)
        stmt = (
            stmt.order_by(DomainEvent.created_at.desc(), DomainEvent.id.desc())
            .offset(max(0, int(offset or 0)))
            .limit(max(1, min(limit, 200)))
        )
        return (await self.session.scalars(stmt)).all()

    async def count_events(
        self,
        org_id: str,
        domain_id: int,
        *,
        record_id: int | None = None,
    ) -> int:
        await self.get_domain(org_id, domain_id)
        stmt = select(func.count()).select_from(DomainEvent).where(
            DomainEvent.org_id == org_id,
            DomainEvent.domain_id == domain_id,
        )
        if record_id is not None:
            stmt = stmt.where(DomainEvent.record_id == record_id)
        return int(await self.session.scalar(stmt) or 0)

    async def get_object_type(
        self,
        domain_id: int,
        object_key: str,
        *,
        for_update: bool = False,
    ) -> DomainObjectType:
        key = _normalize_key(object_key, "object key")
        stmt = select(DomainObjectType).where(
            DomainObjectType.domain_id == domain_id,
            DomainObjectType.key == key,
            DomainObjectType.archived_at.is_(None),
        )
        if for_update:
            stmt = stmt.with_for_update()
        obj = (await self.session.scalars(stmt)).first()
        if obj is None:
            raise DomainNotFound(f"Object type '{key}' not found")
        return obj

    async def get_object_type_by_id(self, object_type_id: int) -> DomainObjectType:
        obj = await self.session.get(DomainObjectType, object_type_id)
        if obj is None or obj.archived_at is not None:
            raise DomainNotFound(f"Object type {object_type_id} not found")
        return obj

    async def get_relation_type(self, domain_id: int, relation_key: str) -> DomainRelationType:
        key = _normalize_key(relation_key, "relation key")
        stmt = select(DomainRelationType).where(
            DomainRelationType.domain_id == domain_id,
            DomainRelationType.key == key,
            DomainRelationType.archived_at.is_(None),
        )
        relation_type = (await self.session.scalars(stmt)).first()
        if relation_type is None:
            raise DomainNotFound(f"Relation type '{key}' not found")
        return relation_type

    async def list_objects(self, domain_id: int) -> Sequence[DomainObjectType]:
        stmt = (
            select(DomainObjectType)
            .where(
                DomainObjectType.domain_id == domain_id,
                DomainObjectType.archived_at.is_(None),
            )
            .order_by(DomainObjectType.sort_order, DomainObjectType.id)
        )
        return (await self.session.scalars(stmt)).all()

    async def list_fields(self, object_type_id: int) -> Sequence[DomainFieldDefinition]:
        stmt = (
            select(DomainFieldDefinition)
            .where(
                DomainFieldDefinition.object_type_id == object_type_id,
                DomainFieldDefinition.archived_at.is_(None),
            )
            .order_by(DomainFieldDefinition.id)
        )
        return (await self.session.scalars(stmt)).all()

    async def list_relation_types(self, domain_id: int) -> Sequence[DomainRelationType]:
        stmt = (
            select(DomainRelationType)
            .where(
                DomainRelationType.domain_id == domain_id,
                DomainRelationType.archived_at.is_(None),
            )
            .order_by(DomainRelationType.id)
        )
        return (await self.session.scalars(stmt)).all()

    def validate_record_data(
        self,
        fields: Iterable[DomainFieldDefinition],
        data: dict[str, Any],
    ) -> dict[str, Any]:
        return _validate_record_data(fields, data)

    async def serialize_domain_summary(self, domain: Domain) -> dict[str, Any]:
        objects = await self.list_objects(domain.id)
        has_records = (
            await self.session.execute(
                select(DomainRecord.id)
                .where(DomainRecord.domain_id == domain.id, DomainRecord.archived_at.is_(None))
                .limit(1)
            )
        ).first()
        return {
            "id": domain.id,
            "org_id": domain.org_id,
            "slug": domain.slug,
            "name": domain.name,
            "description": domain.description,
            "object_count": len(objects),
            "has_records": bool(has_records),
            "archived_at": domain.archived_at,
            "created_at": domain.created_at,
            "updated_at": domain.updated_at,
        }

    async def serialize_domain_schema(self, domain: Domain) -> dict[str, Any]:
        objects = [await self.serialize_object_type(obj) for obj in await self.list_objects(domain.id)]
        return {
            **await self.serialize_domain_summary(domain),
            "objects": objects,
            "relation_types": [
                await self.serialize_relation_type(relation_type)
                for relation_type in await self.list_relation_types(domain.id)
            ],
        }

    async def serialize_object_type(self, obj: DomainObjectType) -> dict[str, Any]:
        return {
            "id": obj.id,
            "domain_id": obj.domain_id,
            "key": obj.key,
            "name": obj.name,
            "description": obj.description,
            "title_field": obj.title_field,
            "sort_order": obj.sort_order,
            "fields": [self.serialize_field(field) for field in await self.list_fields(obj.id)],
            "created_at": obj.created_at,
            "updated_at": obj.updated_at,
        }

    def serialize_field(self, field: DomainFieldDefinition) -> dict[str, Any]:
        return _serialize_field(field)

    async def serialize_relation_type(self, relation_type: DomainRelationType) -> dict[str, Any]:
        source = await self.session.get(DomainObjectType, relation_type.source_object_type_id)
        target = await self.session.get(DomainObjectType, relation_type.target_object_type_id)
        return {
            "id": relation_type.id,
            "domain_id": relation_type.domain_id,
            "key": relation_type.key,
            "name": relation_type.name,
            "description": relation_type.description,
            "source_object": source.key if source else None,
            "target_object": target.key if target else None,
            "source_object_type_id": relation_type.source_object_type_id,
            "target_object_type_id": relation_type.target_object_type_id,
            "cardinality": relation_type.cardinality,
            "created_at": relation_type.created_at,
            "updated_at": relation_type.updated_at,
        }

    async def serialize_record(self, record: DomainRecord) -> dict[str, Any]:
        obj = await self.session.get(DomainObjectType, record.object_type_id)
        return {
            "id": record.id,
            "org_id": record.org_id,
            "domain_id": record.domain_id,
            "object_type_id": record.object_type_id,
            "object_key": obj.key if obj else None,
            "title": record.title,
            "data": record.data or {},
            "version": record.version,
            "archived_at": record.archived_at,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
        }

    async def serialize_record_compact(
        self,
        record: DomainRecord,
        *,
        fields: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        obj = await self.session.get(DomainObjectType, record.object_type_id)
        data = record.data if isinstance(record.data, dict) else {}
        if fields is None:
            projected_data = {
                key: value
                for key, value in data.items()
                if value is None
                or isinstance(value, bool | int | float)
                or isinstance(value, str) and len(value) <= 120
            }
        else:
            projected_data = {}
            for key in fields:
                if key not in data:
                    continue
                value = data[key]
                if isinstance(value, dict | list):
                    value = json.dumps(value, default=str)
                if isinstance(value, str) and len(value) > 200:
                    value = f"{value[:200]}…"
                projected_data[key] = value

        payload = {
            "id": record.id,
            "object_key": obj.key if obj else None,
            "title": record.title,
            "version": record.version,
            "updated_at": record.updated_at,
        }
        if record.archived_at is not None:
            payload["archived_at"] = record.archived_at
        payload["data"] = projected_data
        return payload

    async def serialize_relation(self, relation: DomainRelation) -> dict[str, Any]:
        relation_type = await self.session.get(DomainRelationType, relation.relation_type_id)
        return {
            "id": relation.id,
            "org_id": relation.org_id,
            "domain_id": relation.domain_id,
            "relation_type_id": relation.relation_type_id,
            "relation_key": relation_type.key if relation_type else None,
            "source_record_id": relation.source_record_id,
            "target_record_id": relation.target_record_id,
            "properties": relation.properties or {},
            "archived_at": relation.archived_at,
            "created_at": relation.created_at,
            "updated_at": relation.updated_at,
        }

    def serialize_event(self, event: DomainEvent) -> dict[str, Any]:
        return _serialize_event(event)

    async def _add_event(
        self,
        *,
        org_id: str,
        domain_id: int,
        event_type: str,
        actor_id: str | None = None,
        actor_kind: str = "human",
        record_id: int | None = None,
        relation_id: int | None = None,
        run_id: int | None = None,
        idea_id: str | None = None,
        before: dict[str, Any] | None = None,
        after: dict[str, Any] | None = None,
        patch: dict[str, Any] | None = None,
        reason: str | None = None,
    ) -> DomainEvent:
        event = DomainEvent(
            org_id=org_id,
            domain_id=domain_id,
            record_id=record_id,
            relation_id=relation_id,
            event_type=event_type,
            actor_kind=actor_kind,
            actor_id=actor_id,
            run_id=run_id,
            idea_id=_uuid_or_none(idea_id),
            before=_json_safe(before or {}),
            after=_json_safe(after or {}),
            patch=_json_safe(patch or {}),
            reason=reason,
        )
        self.session.add(event)
        await self.session.flush()
        return event

    async def _ensure_slug_available(self, org_id: str, slug: str) -> None:
        existing = (
            await self.session.scalars(
                select(Domain.id).where(Domain.org_id == org_id, Domain.slug == slug)
            )
        ).first()
        if existing is not None:
            raise DomainError(f"Domain slug '{slug}' already exists")

    async def _ensure_object_key_available(self, domain_id: int, key: str) -> None:
        existing = (
            await self.session.scalars(
                select(DomainObjectType.id).where(
                    DomainObjectType.domain_id == domain_id,
                    DomainObjectType.key == key,
                )
            )
        ).first()
        if existing is not None:
            raise DomainError(f"Object key '{key}' already exists")

    async def _ensure_field_key_available(self, object_type_id: int, key: str) -> None:
        existing = (
            await self.session.scalars(
                select(DomainFieldDefinition.id).where(
                    DomainFieldDefinition.object_type_id == object_type_id,
                    DomainFieldDefinition.key == key,
                )
            )
        ).first()
        if existing is not None:
            raise DomainError(f"Field key '{key}' already exists")

    async def _ensure_relation_type_key_available(self, domain_id: int, key: str) -> None:
        existing = (
            await self.session.scalars(
                select(DomainRelationType.id).where(
                    DomainRelationType.domain_id == domain_id,
                    DomainRelationType.key == key,
                )
            )
        ).first()
        if existing is not None:
            raise DomainError(f"Relation key '{key}' already exists")

    async def _object_map(self, domain_id: int) -> dict[str, DomainObjectType]:
        return {obj.key: obj for obj in await self.list_objects(domain_id)}

    async def _domain_org_id(self, domain_id: int) -> str:
        domain = await self.session.get(Domain, domain_id)
        if domain is None:
            raise DomainNotFound(f"Domain {domain_id} not found")
        return domain.org_id


def _normalize_key(value: Any, field_name: str) -> str:
    key = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    key = re.sub(r"[^a-z0-9_]+", "", key)
    key = re.sub(r"_+", "_", key).strip("_")
    if not key or not KEY_RE.fullmatch(key):
        raise DomainError(f"{field_name} must start with a letter and use lowercase letters, numbers, or underscores")
    return key


def _normalize_slug(value: str) -> str:
    slug = str(value or "").strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug).strip("-")
    slug = re.sub(r"-+", "-", slug)
    slug = slug[:80].rstrip("-")
    if not slug or not SLUG_RE.fullmatch(slug):
        raise DomainError("slug must use lowercase letters, numbers, and hyphens")
    return slug


def _normalize_field_type(value: Any) -> str:
    field_type = str(value or "").strip().lower()
    if field_type not in SUPPORTED_FIELD_TYPES:
        raise DomainFieldTypeError(field_type)
    return field_type


def _nonempty(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise DomainError(f"{field_name} is required")
    return text


def _clean_optional(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _title_from_key(key: str) -> str:
    return key.replace("_", " ").title()


def _is_empty(value: Any) -> bool:
    return value is None or value == ""


def _tracker_pr_natural_key(
    fields: Iterable[DomainFieldDefinition],
    data: Mapping[str, Any],
) -> tuple[str, str] | None:
    field_keys = {field.key for field in fields}
    if not {"repo", "pr_number"}.issubset(field_keys):
        return None

    repo = _normalize_repo_identity(data.get("repo"))
    pr_number = _normalize_pr_number(data.get("pr_number"))
    if repo and pr_number:
        return repo, pr_number

    if "pr_url" in field_keys:
        return _github_pr_key_from_url(data.get("pr_url"))
    return None


def _normalize_repo_identity(value: Any) -> str | None:
    repo = str(value or "").strip().rstrip("/")
    if not repo:
        return None
    repo = re.sub(r"^git@github\.com:", "", repo, flags=re.IGNORECASE)
    repo = re.sub(r"^(?:https?://)?github\.com/", "", repo, flags=re.IGNORECASE)
    repo = repo.rstrip("/")
    if repo.lower().endswith(".git"):
        repo = repo[:-4]
    return repo.casefold() or None


def _normalize_pr_number(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    pr_number = str(value).strip().removeprefix("#").strip()
    if not pr_number:
        return None
    if pr_number.isdigit():
        return str(int(pr_number))
    return pr_number.casefold()


def _github_pr_key_from_url(value: Any) -> tuple[str, str] | None:
    pr_url = str(value or "").strip()
    match = re.match(
        r"^https?://github\.com/([^/]+)/([^/]+)/pull/([^/?#]+)",
        pr_url,
        flags=re.IGNORECASE,
    )
    if match is None:
        return None
    repo = _normalize_repo_identity(f"{match.group(1)}/{match.group(2)}")
    pr_number = _normalize_pr_number(match.group(3))
    if not repo or not pr_number:
        return None
    return repo, pr_number


def _coerce_field_value(field: DomainFieldDefinition, value: Any) -> Any:
    kind = field.field_type
    if kind in {"text", "long_text", "url", "email", "phone", "user_ref"}:
        return str(value).strip()
    if kind in {"number", "money"}:
        if isinstance(value, bool):
            raise DomainError(f"Field '{field.key}' must be a number")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise DomainError(f"Field '{field.key}' must be a number") from exc
        return int(number) if number.is_integer() else number
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in {"true", "false"}:
            return value.lower() == "true"
        raise DomainError(f"Field '{field.key}' must be true or false")
    if kind == "date":
        text = str(value).strip()
        try:
            date.fromisoformat(text)
        except ValueError as exc:
            raise DomainError(f"Field '{field.key}' must be an ISO date") from exc
        return text
    if kind == "datetime":
        text = str(value).strip()
        try:
            datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            raise DomainError(f"Field '{field.key}' must be an ISO datetime") from exc
        return text
    if kind == "enum":
        text = str(value).strip()
        if text not in (field.options or []):
            raise DomainError(f"Field '{field.key}' must be one of: {', '.join(field.options or [])}")
        return text
    if kind == "multi_enum":
        if not isinstance(value, list):
            raise DomainError(f"Field '{field.key}' must be a list")
        options = set(field.options or [])
        normalized = [str(item).strip() for item in value]
        invalid = [item for item in normalized if item not in options]
        if invalid:
            raise DomainError(f"Field '{field.key}' has invalid option(s): {', '.join(invalid)}")
        return normalized
    if kind == "record_ref":
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise DomainError(f"Field '{field.key}' must be a record id") from exc
    if kind == "json":
        return value
    raise DomainError(f"Unsupported field type: {kind}")


def _validate_field_constraints(field: DomainFieldDefinition, value: Any) -> None:
    validation = field.validation or {}
    if not isinstance(validation, Mapping):
        raise DomainError(f"Field '{field.key}' has an invalid validation contract")
    _validate_constraint_value(field.key, value, validation, path=field.key)


def _validate_constraint_value(
    field_key: str,
    value: Any,
    constraints: Mapping[str, Any],
    *,
    path: str,
) -> None:
    expected_type = constraints.get("type")
    if expected_type is not None and not _matches_validation_type(value, expected_type):
        raise DomainError(
            f"Field '{field_key}' value at '{path}' must be {_validation_type_label(expected_type)}"
        )

    allowed_values = constraints.get("enum")
    if isinstance(allowed_values, list) and value not in allowed_values:
        raise DomainError(
            f"Field '{field_key}' value at '{path}' must be one of: "
            f"{', '.join(str(item) for item in allowed_values)}"
        )

    if isinstance(value, str | list | dict):
        min_length = constraints.get("min_length", constraints.get("minLength"))
        max_length = constraints.get("max_length", constraints.get("maxLength"))
        if min_length is not None and len(value) < int(min_length):
            raise DomainError(
                f"Field '{field_key}' value at '{path}' must contain at least {int(min_length)} item(s)"
            )
        if max_length is not None and len(value) > int(max_length):
            raise DomainError(
                f"Field '{field_key}' value at '{path}' must contain at most {int(max_length)} item(s)"
            )

    pattern = constraints.get("pattern")
    if pattern is not None and isinstance(value, str):
        try:
            matches = re.search(str(pattern), value) is not None
        except re.error as exc:
            raise DomainError(f"Field '{field_key}' has an invalid validation pattern") from exc
        if not matches:
            raise DomainError(f"Field '{field_key}' value at '{path}' has an invalid format")

    if isinstance(value, list):
        item_constraints = constraints.get("items")
        if isinstance(item_constraints, Mapping):
            for index, item in enumerate(value):
                _validate_constraint_value(
                    field_key,
                    item,
                    item_constraints,
                    path=f"{path}[{index}]",
                )

    if not isinstance(value, Mapping):
        return

    required_keys = constraints.get("required")
    if isinstance(required_keys, list):
        missing = [key for key in required_keys if _is_empty(value.get(str(key)))]
        if missing:
            raise DomainError(
                f"Field '{field_key}' value at '{path}' requires: "
                f"{', '.join(str(key) for key in missing)}"
            )

    properties = constraints.get("properties")
    if isinstance(properties, Mapping):
        if constraints.get("additional_properties") is False:
            unknown = sorted(set(value) - set(properties))
            if unknown:
                raise DomainError(
                    f"Field '{field_key}' value at '{path}' has unknown key(s): "
                    f"{', '.join(str(key) for key in unknown)}"
                )
        for key, property_constraints in properties.items():
            if key not in value or not isinstance(property_constraints, Mapping):
                continue
            _validate_constraint_value(
                field_key,
                value[key],
                property_constraints,
                path=f"{path}.{key}",
            )

    source_ref_patterns = constraints.get("source_ref_patterns")
    if isinstance(source_ref_patterns, Mapping):
        source = value.get("source")
        ref_pattern = source_ref_patterns.get(source)
        ref = value.get("ref")
        if ref_pattern is not None and (
            not isinstance(ref, str) or re.fullmatch(str(ref_pattern), ref) is None
        ):
            raise DomainError(
                f"Field '{field_key}' value at '{path}.ref' has an invalid format for source '{source}'"
            )


def _matches_validation_type(value: Any, expected_type: Any) -> bool:
    return {
        "array": isinstance(value, list),
        "object": isinstance(value, Mapping),
        "string": isinstance(value, str),
        "number": isinstance(value, int | float) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
    }.get(str(expected_type), False)


def _validation_type_label(expected_type: Any) -> str:
    return {
        "array": "an array",
        "object": "an object",
        "string": "a string",
        "number": "a number",
        "boolean": "a boolean",
    }.get(str(expected_type), f"a value of type '{expected_type}'")


def _validate_immutable_field_updates(
    fields: Iterable[DomainFieldDefinition],
    current_data: Mapping[str, Any],
    data_patch: Mapping[str, Any],
) -> None:
    for field in fields:
        validation = field.validation or {}
        if (
            not isinstance(validation, Mapping)
            or not validation.get("immutable")
            or field.key not in data_patch
        ):
            continue
        requested = _coerce_field_value(field, data_patch[field.key])
        _validate_field_constraints(field, requested)
        if requested != current_data.get(field.key):
            raise DomainError(f"Field '{field.key}' is immutable once set")


def _record_title(
    obj: DomainObjectType,
    fields: Sequence[DomainFieldDefinition],
    data: dict[str, Any],
    *,
    title: str | None = None,
    record_id: int | None = None,
) -> str:
    if title and title.strip():
        return title.strip()
    if obj.title_field and data.get(obj.title_field):
        return str(data[obj.title_field])
    for field in fields:
        if field.field_type in {"text", "email", "url", "phone"} and data.get(field.key):
            return str(data[field.key])
    return f"{obj.name} #{record_id}" if record_id else obj.name


def _record_search_text(
    title: str,
    fields: Sequence[DomainFieldDefinition],
    data: dict[str, Any],
) -> str:
    parts = [title]
    for field in fields:
        if not field.searchable:
            continue
        value = data.get(field.key)
        if value is None:
            continue
        if isinstance(value, list):
            parts.extend(str(item) for item in value)
        elif isinstance(value, dict):
            continue
        else:
            parts.append(str(value))
    return " ".join(part.strip() for part in parts if part and str(part).strip()).lower()


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value
