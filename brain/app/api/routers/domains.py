"""Domains router."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from brain.app.api.auth import get_current_user
from brain.app.api.authorization import (
    can_manage_domains,
    can_write_domains,
    require_org_context,
)
from brain.app.api.deps import get_db, rate_limit
from brain.app.api.db_utils import run_db
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
from brain.systems.user_domains.service import DomainError, DomainNotFound, DomainService

router = APIRouter(
    prefix="/api/domains",
    tags=["domains"],
    dependencies=[Depends(rate_limit)],
)


def _service(db: Session) -> DomainService:
    return DomainService(db)


async def _run_db(db: AsyncSession, fn):
    return await run_db(db, fn)


def _domain_error(exc: Exception) -> HTTPException:
    if isinstance(exc, DomainNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, DomainError):
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
    def _list(sync_db: Session):
        org_id = require_org_context(user)
        service = _service(sync_db)
        return [service.serialize_domain_summary(domain) for domain in service.list_domains(org_id)]

    return await _run_db(db, _list)


@router.post("", response_model=DomainSchemaRead, status_code=201, include_in_schema=False)
@router.post("/", response_model=DomainSchemaRead, status_code=201)
async def create_domain(
    body: DomainCreate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _create(sync_db: Session):
        _require_domain_manage(user)
        org_id = require_org_context(user)
        service = _service(sync_db)
        try:
            domain = service.create_domain(
                org_id,
                name=body.name,
                slug=body.slug,
                description=body.description,
                objects=[obj.model_dump() for obj in body.objects],
                relations=[relation.model_dump() for relation in body.relations],
                actor_id=str(user.get("id")) if user.get("id") else None,
                actor_kind="human",
            )
            sync_db.flush()
            return service.serialize_domain_schema(domain)
        except (DomainError, DomainNotFound) as exc:
            raise _domain_error(exc) from exc

    return await _run_db(db, _create)


@router.get("/{domain_id}", response_model=DomainSchemaRead)
async def get_domain_schema(
    domain_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _get(sync_db: Session):
        org_id = require_org_context(user)
        service = _service(sync_db)
        try:
            return service.serialize_domain_schema(service.get_domain(org_id, domain_id))
        except (DomainError, DomainNotFound) as exc:
            raise _domain_error(exc) from exc

    return await _run_db(db, _get)


@router.delete("/{domain_id}")
async def remove_domain(
    domain_id: int,
    body: DomainRemove | None = None,
    mode: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _remove(sync_db: Session):
        _require_domain_manage(user)
        org_id = require_org_context(user)
        service = _service(sync_db)
        selected_mode = mode or (body.mode if body else "archive")
        try:
            return service.remove_domain(
                org_id,
                domain_id,
                mode=selected_mode,
                actor_id=str(user.get("id")) if user.get("id") else None,
                actor_kind="human",
            )
        except (DomainError, DomainNotFound) as exc:
            raise _domain_error(exc) from exc

    return await _run_db(db, _remove)


@router.post("/{domain_id}/objects", response_model=DomainSchemaRead)
async def add_domain_object(
    domain_id: int,
    body: DomainObjectAdd,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _add(sync_db: Session):
        _require_domain_manage(user)
        org_id = require_org_context(user)
        service = _service(sync_db)
        try:
            domain = service.get_domain(org_id, domain_id)
            service.add_object_type(
                domain,
                body.model_dump(),
                actor_id=str(user.get("id")) if user.get("id") else None,
            )
            sync_db.flush()
            return service.serialize_domain_schema(domain)
        except (DomainError, DomainNotFound) as exc:
            raise _domain_error(exc) from exc

    return await _run_db(db, _add)


@router.post("/{domain_id}/objects/{object_key}/fields", response_model=DomainSchemaRead)
async def add_domain_field(
    domain_id: int,
    object_key: str,
    body: DomainFieldAdd,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _add(sync_db: Session):
        _require_domain_manage(user)
        org_id = require_org_context(user)
        service = _service(sync_db)
        try:
            domain = service.get_domain(org_id, domain_id)
            obj = service.get_object_type(domain.id, object_key)
            service.add_field_definition(obj, body.model_dump())
            sync_db.flush()
            return service.serialize_domain_schema(domain)
        except (DomainError, DomainNotFound) as exc:
            raise _domain_error(exc) from exc

    return await _run_db(db, _add)


@router.post("/{domain_id}/relation-types", response_model=DomainSchemaRead)
async def add_domain_relation_type(
    domain_id: int,
    body: DomainRelationTypeAdd,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _add(sync_db: Session):
        _require_domain_manage(user)
        org_id = require_org_context(user)
        service = _service(sync_db)
        try:
            domain = service.get_domain(org_id, domain_id)
            service.add_relation_type(
                domain,
                body.model_dump(),
                actor_id=str(user.get("id")) if user.get("id") else None,
            )
            sync_db.flush()
            return service.serialize_domain_schema(domain)
        except (DomainError, DomainNotFound) as exc:
            raise _domain_error(exc) from exc

    return await _run_db(db, _add)


@router.get("/{domain_id}/records", response_model=list[DomainRecordRead])
async def list_domain_records(
    domain_id: int,
    object_key: str | None = None,
    search: str | None = None,
    include_archived: bool = False,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _list(sync_db: Session):
        org_id = require_org_context(user)
        service = _service(sync_db)
        try:
            return [
                service.serialize_record(record)
                for record in service.list_records(
                    org_id,
                    domain_id,
                    object_key=object_key,
                    search=search,
                    include_archived=include_archived,
                    limit=limit,
                )
            ]
        except (DomainError, DomainNotFound) as exc:
            raise _domain_error(exc) from exc

    return await _run_db(db, _list)


@router.post("/{domain_id}/objects/{object_key}/records", response_model=DomainRecordRead, status_code=201)
async def create_domain_record(
    domain_id: int,
    object_key: str,
    body: DomainRecordCreate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _create(sync_db: Session):
        _require_domain_write(user)
        org_id = require_org_context(user)
        service = _service(sync_db)
        try:
            record = service.create_record(
                org_id,
                domain_id,
                object_key,
                data=body.data,
                title=body.title,
                actor_id=str(user.get("id")) if user.get("id") else None,
                actor_kind="human",
            )
            sync_db.flush()
            return service.serialize_record(record)
        except (DomainError, DomainNotFound) as exc:
            raise _domain_error(exc) from exc

    return await _run_db(db, _create)


@router.get("/{domain_id}/records/{record_id}", response_model=DomainRecordRead)
async def get_domain_record(
    domain_id: int,
    record_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _get(sync_db: Session):
        org_id = require_org_context(user)
        service = _service(sync_db)
        try:
            return service.serialize_record(service.get_record(org_id, domain_id, record_id))
        except (DomainError, DomainNotFound) as exc:
            raise _domain_error(exc) from exc

    return await _run_db(db, _get)


@router.patch("/{domain_id}/records/{record_id}", response_model=DomainRecordRead)
async def update_domain_record(
    domain_id: int,
    record_id: int,
    body: DomainRecordUpdate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _update(sync_db: Session):
        _require_domain_write(user)
        org_id = require_org_context(user)
        service = _service(sync_db)
        try:
            record = service.update_record(
                org_id,
                domain_id,
                record_id,
                data_patch=body.data_patch,
                title=body.title,
                expected_version=body.expected_version,
                actor_id=str(user.get("id")) if user.get("id") else None,
                actor_kind="human",
            )
            sync_db.flush()
            return service.serialize_record(record)
        except (DomainError, DomainNotFound) as exc:
            raise _domain_error(exc) from exc

    return await _run_db(db, _update)


@router.delete("/{domain_id}/records/{record_id}")
async def remove_domain_record(
    domain_id: int,
    record_id: int,
    body: DomainRemoveRecord | None = None,
    mode: str | None = None,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _remove(sync_db: Session):
        _require_domain_write(user)
        org_id = require_org_context(user)
        service = _service(sync_db)
        selected_mode = mode or (body.mode if body else "archive")
        try:
            return service.remove_record(
                org_id,
                domain_id,
                record_id,
                mode=selected_mode,
                actor_id=str(user.get("id")) if user.get("id") else None,
                actor_kind="human",
            )
        except (DomainError, DomainNotFound) as exc:
            raise _domain_error(exc) from exc

    return await _run_db(db, _remove)


@router.post("/{domain_id}/relations", response_model=DomainRelationRead, status_code=201)
async def create_domain_relation(
    domain_id: int,
    body: DomainRelationCreate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _create(sync_db: Session):
        _require_domain_write(user)
        org_id = require_org_context(user)
        service = _service(sync_db)
        try:
            relation = service.create_relation(
                org_id,
                domain_id,
                body.relation_key,
                source_record_id=body.source_record_id,
                target_record_id=body.target_record_id,
                properties=body.properties,
                actor_id=str(user.get("id")) if user.get("id") else None,
                actor_kind="human",
            )
            sync_db.flush()
            return service.serialize_relation(relation)
        except (DomainError, DomainNotFound) as exc:
            raise _domain_error(exc) from exc

    return await _run_db(db, _create)


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
    def _list(sync_db: Session):
        org_id = require_org_context(user)
        service = _service(sync_db)
        try:
            return [
                service.serialize_relation(relation)
                for relation in service.list_relations(
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

    return await _run_db(db, _list)


@router.delete("/{domain_id}/relations/{relation_id}")
async def remove_domain_relation(
    domain_id: int,
    relation_id: int,
    mode: str = "archive",
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _remove(sync_db: Session):
        _require_domain_write(user)
        org_id = require_org_context(user)
        service = _service(sync_db)
        try:
            return service.remove_relation(
                org_id,
                domain_id,
                relation_id,
                mode=mode,
                actor_id=str(user.get("id")) if user.get("id") else None,
                actor_kind="human",
            )
        except (DomainError, DomainNotFound) as exc:
            raise _domain_error(exc) from exc

    return await _run_db(db, _remove)


@router.get("/{domain_id}/events", response_model=list[DomainEventRead])
async def list_domain_events(
    domain_id: int,
    record_id: int | None = None,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    def _list(sync_db: Session):
        org_id = require_org_context(user)
        service = _service(sync_db)
        try:
            return [
                service.serialize_event(event)
                for event in service.list_events(org_id, domain_id, record_id=record_id, limit=limit)
            ]
        except (DomainError, DomainNotFound) as exc:
            raise _domain_error(exc) from exc

    return await _run_db(db, _list)
