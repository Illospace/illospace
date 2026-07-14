"""Domains router."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.ext.asyncio import AsyncSession

from brain.app.api.auth import get_current_user
from brain.app.api.authorization import (
    can_manage_domains,
    can_write_domains,
    require_org_context,
)
from brain.app.api.deps import get_db, rate_limit
from brain.app.api.schemas.domains import (
    DomainCreate,
    DomainEventRead,
    DomainFieldAdd,
    DomainObjectAdd,
    DomainRecordCreate,
    DomainRecordRead,
    DomainRecordUpdate,
    DomainRemove,
    DomainRelationCreate,
    DomainRelationRead,
    DomainRelationTypeAdd,
    DomainRemoveRecord,
    DomainSchemaRead,
    DomainSummaryRead,
)
from brain.kernel.common.pagination import InvalidPageToken, next_offset_token, page_offset
from brain.systems.user_domains.service import AsyncDomainService, DomainError, DomainNotFound

router = APIRouter(
    prefix="/api/domains",
    tags=["domains"],
    dependencies=[Depends(rate_limit)],
)


def _service(db: AsyncSession) -> AsyncDomainService:
    return AsyncDomainService(db)


def _domain_error(exc: Exception) -> HTTPException:
    if isinstance(exc, DomainNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (DomainError, InvalidPageToken)):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


def _require_domain_write(user: dict[str, Any]) -> None:
    if not can_write_domains(user):
        raise HTTPException(status_code=403, detail="Permission denied")


def _require_domain_manage(user: dict[str, Any]) -> None:
    if not can_manage_domains(user):
        raise HTTPException(status_code=403, detail="Permission denied")


@router.get("", response_model=list[DomainSummaryRead], include_in_schema=False)
@router.get("/", response_model=list[DomainSummaryRead])
async def list_domains(
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    org_id = require_org_context(user)
    service = _service(db)
    return [await service.serialize_domain_summary(domain) for domain in await service.list_domains(org_id)]


@router.post("", response_model=DomainSchemaRead, status_code=201, include_in_schema=False)
@router.post("/", response_model=DomainSchemaRead, status_code=201)
async def create_domain(
    body: DomainCreate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    _require_domain_manage(user)
    org_id = require_org_context(user)
    service = _service(db)
    try:
        domain = await service.create_domain(
            org_id,
            name=body.name,
            slug=body.slug,
            description=body.description,
            objects=[obj.model_dump() for obj in body.objects],
            relations=[relation.model_dump() for relation in body.relations],
            actor_id=str(user.get("id")) if user.get("id") else None,
            actor_kind="human",
        )
        return await service.serialize_domain_schema(domain)
    except (DomainError, DomainNotFound) as exc:
        raise _domain_error(exc) from exc


@router.get("/{domain_id}", response_model=DomainSchemaRead)
async def get_domain_schema(
    domain_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    org_id = require_org_context(user)
    service = _service(db)
    try:
        return await service.serialize_domain_schema(await service.get_domain(org_id, domain_id))
    except (DomainError, DomainNotFound) as exc:
        raise _domain_error(exc) from exc


@router.delete("/{domain_id}")
async def remove_domain(
    domain_id: int,
    body: DomainRemove | None = None,
    mode: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    _require_domain_manage(user)
    org_id = require_org_context(user)
    service = _service(db)
    selected_mode = mode or (body.mode if body else "archive")
    try:
        return await service.remove_domain(
            org_id,
            domain_id,
            mode=selected_mode,
            actor_id=str(user.get("id")) if user.get("id") else None,
            actor_kind="human",
        )
    except (DomainError, DomainNotFound) as exc:
        raise _domain_error(exc) from exc


@router.post("/{domain_id}/objects", response_model=DomainSchemaRead)
async def add_domain_object(
    domain_id: int,
    body: DomainObjectAdd,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    _require_domain_manage(user)
    org_id = require_org_context(user)
    service = _service(db)
    try:
        domain = await service.get_domain(org_id, domain_id)
        await service.add_object_type(
            domain,
            body.model_dump(),
            actor_id=str(user.get("id")) if user.get("id") else None,
        )
        return await service.serialize_domain_schema(domain)
    except (DomainError, DomainNotFound) as exc:
        raise _domain_error(exc) from exc


@router.post("/{domain_id}/objects/{object_key}/fields", response_model=DomainSchemaRead)
async def add_domain_field(
    domain_id: int,
    object_key: str,
    body: DomainFieldAdd,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    _require_domain_manage(user)
    org_id = require_org_context(user)
    service = _service(db)
    try:
        domain = await service.get_domain(org_id, domain_id)
        obj = await service.get_object_type(domain.id, object_key)
        await service.add_field_definition(obj, body.model_dump())
        return await service.serialize_domain_schema(domain)
    except (DomainError, DomainNotFound) as exc:
        raise _domain_error(exc) from exc


@router.post("/{domain_id}/relation-types", response_model=DomainSchemaRead)
async def add_domain_relation_type(
    domain_id: int,
    body: DomainRelationTypeAdd,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    _require_domain_manage(user)
    org_id = require_org_context(user)
    service = _service(db)
    try:
        domain = await service.get_domain(org_id, domain_id)
        await service.add_relation_type(
            domain,
            body.model_dump(),
            actor_id=str(user.get("id")) if user.get("id") else None,
        )
        return await service.serialize_domain_schema(domain)
    except (DomainError, DomainNotFound) as exc:
        raise _domain_error(exc) from exc


@router.get("/{domain_id}/records", response_model=list[DomainRecordRead])
async def list_domain_records(
    domain_id: int,
    response: Response,
    object_key: str | None = None,
    search: str | None = None,
    include_archived: bool = False,
    limit: int = 100,
    cursor: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    org_id = require_org_context(user)
    service = _service(db)
    try:
        page_limit = max(1, min(int(limit or 100), 500))
        page_kind = f"domain_api:records:{domain_id}"
        offset = page_offset(cursor, kind=page_kind)
        records = [
            await service.serialize_record(record)
            for record in await service.list_records(
                org_id,
                domain_id,
                object_key=object_key,
                search=search,
                include_archived=include_archived,
                limit=page_limit,
                offset=offset,
            )
        ]
        total = await service.count_records(
            org_id,
            domain_id,
            object_key=object_key,
            search=search,
            include_archived=include_archived,
        )
        has_more = offset + len(records) < total
        response.headers["X-Truncated"] = str(has_more).lower()
        response.headers["X-Evidence-Health"] = "ok"
        if has_more:
            response.headers["X-Next-Page"] = next_offset_token(
                kind=page_kind,
                offset=offset,
                returned=len(records),
            )
        return records
    except (DomainError, DomainNotFound, InvalidPageToken) as exc:
        raise _domain_error(exc) from exc


@router.post("/{domain_id}/objects/{object_key}/records", response_model=DomainRecordRead, status_code=201)
async def create_domain_record(
    domain_id: int,
    object_key: str,
    body: DomainRecordCreate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    _require_domain_write(user)
    org_id = require_org_context(user)
    service = _service(db)
    try:
        record = await service.create_record(
            org_id,
            domain_id,
            object_key,
            data=body.data,
            title=body.title,
            actor_id=str(user.get("id")) if user.get("id") else None,
            actor_kind="human",
        )
        return await service.serialize_record(record)
    except (DomainError, DomainNotFound) as exc:
        raise _domain_error(exc) from exc


@router.get("/{domain_id}/records/{record_id}", response_model=DomainRecordRead)
async def get_domain_record(
    domain_id: int,
    record_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    org_id = require_org_context(user)
    service = _service(db)
    try:
        return await service.serialize_record(await service.get_record(org_id, domain_id, record_id))
    except (DomainError, DomainNotFound) as exc:
        raise _domain_error(exc) from exc


@router.patch("/{domain_id}/records/{record_id}", response_model=DomainRecordRead)
async def update_domain_record(
    domain_id: int,
    record_id: int,
    body: DomainRecordUpdate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    _require_domain_write(user)
    org_id = require_org_context(user)
    service = _service(db)
    try:
        record = await service.update_record(
            org_id,
            domain_id,
            record_id,
            data_patch=body.data_patch,
            title=body.title,
            expected_version=body.expected_version,
            actor_id=str(user.get("id")) if user.get("id") else None,
            actor_kind="human",
        )
        return await service.serialize_record(record)
    except (DomainError, DomainNotFound) as exc:
        raise _domain_error(exc) from exc


@router.delete("/{domain_id}/records/{record_id}")
async def remove_domain_record(
    domain_id: int,
    record_id: int,
    body: DomainRemoveRecord | None = None,
    mode: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    _require_domain_write(user)
    org_id = require_org_context(user)
    service = _service(db)
    selected_mode = mode or (body.mode if body else "archive")
    try:
        return await service.remove_record(
            org_id,
            domain_id,
            record_id,
            mode=selected_mode,
            actor_id=str(user.get("id")) if user.get("id") else None,
            actor_kind="human",
        )
    except (DomainError, DomainNotFound) as exc:
        raise _domain_error(exc) from exc


@router.post("/{domain_id}/relations", response_model=DomainRelationRead, status_code=201)
async def create_domain_relation(
    domain_id: int,
    body: DomainRelationCreate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    _require_domain_write(user)
    org_id = require_org_context(user)
    service = _service(db)
    try:
        relation = await service.create_relation(
            org_id,
            domain_id,
            body.relation_key,
            source_record_id=body.source_record_id,
            target_record_id=body.target_record_id,
            properties=body.properties,
            actor_id=str(user.get("id")) if user.get("id") else None,
            actor_kind="human",
        )
        return await service.serialize_relation(relation)
    except (DomainError, DomainNotFound) as exc:
        raise _domain_error(exc) from exc


@router.get("/{domain_id}/relations", response_model=list[DomainRelationRead])
async def list_domain_relations(
    domain_id: int,
    relation_key: str | None = None,
    source_record_id: int | None = None,
    target_record_id: int | None = None,
    include_archived: bool = False,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    org_id = require_org_context(user)
    service = _service(db)
    try:
        return [
            await service.serialize_relation(relation)
            for relation in await service.list_relations(
                org_id,
                domain_id,
                relation_key=relation_key,
                source_record_id=source_record_id,
                target_record_id=target_record_id,
                include_archived=include_archived,
                limit=limit,
            )
        ]
    except (DomainError, DomainNotFound) as exc:
        raise _domain_error(exc) from exc


@router.delete("/{domain_id}/relations/{relation_id}")
async def remove_domain_relation(
    domain_id: int,
    relation_id: int,
    mode: str = "archive",
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    _require_domain_write(user)
    org_id = require_org_context(user)
    service = _service(db)
    try:
        return await service.remove_relation(
            org_id,
            domain_id,
            relation_id,
            mode=mode,
            actor_id=str(user.get("id")) if user.get("id") else None,
            actor_kind="human",
        )
    except (DomainError, DomainNotFound) as exc:
        raise _domain_error(exc) from exc


@router.get("/{domain_id}/events", response_model=list[DomainEventRead])
async def list_domain_events(
    domain_id: int,
    response: Response,
    record_id: int | None = None,
    limit: int = 50,
    cursor: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    org_id = require_org_context(user)
    service = _service(db)
    try:
        page_limit = max(1, min(int(limit or 50), 200))
        page_kind = f"domain_api:events:{domain_id}"
        offset = page_offset(cursor, kind=page_kind)
        events = [
            service.serialize_event(event)
            for event in await service.list_events(
                org_id,
                domain_id,
                record_id=record_id,
                limit=page_limit,
                offset=offset,
            )
        ]
        total = await service.count_events(org_id, domain_id, record_id=record_id)
        has_more = offset + len(events) < total
        response.headers["X-Truncated"] = str(has_more).lower()
        response.headers["X-Evidence-Health"] = "ok"
        if has_more:
            response.headers["X-Next-Page"] = next_offset_token(
                kind=page_kind,
                offset=offset,
                returned=len(events),
            )
        return events
    except (DomainError, DomainNotFound, InvalidPageToken) as exc:
        raise _domain_error(exc) from exc
