"""Cycles router."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.app.api.auth import get_current_user
from brain.app.api.deps import get_db, rate_limit
from brain.app.api.schemas.cycles import CycleCreate, CycleRead, CycleRunRead, CycleUpdate
from brain.systems.cycles.access import (
    CycleActor,
    cycle_scope_conditions,
    target_idea_scope_conditions,
)
from brain.systems.cycles.commands import (
    UNSET_CYCLE_FIELD,
    async_create_cycle as command_create_cycle,
    async_delete_cycle as command_delete_cycle,
    async_update_cycle as command_update_cycle,
    cycle_change_event,
)
from brain.systems.cycles.events import publish_cycle_change
from brain.systems.cycles.common import SCHEDULED_DIGEST_RUN_KIND
from brain.systems.cycles.serializers import serialize_cycle, serialize_cycle_run
from brain.systems.cycles.service import async_run_cycle_now
from brain.platform.db.models.cycle import Cycle, CycleRun
from brain.platform.db.models.idea import Idea

router = APIRouter(
    prefix="/api/cycles",
    tags=["cycles"],
    dependencies=[Depends(rate_limit)],
)


def _bad_request(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


async def _get_cycle_or_404(db: AsyncSession, cycle_id: int, user: dict[str, Any]) -> Cycle:
    stmt = select(Cycle).where(
        Cycle.id == cycle_id,
        *cycle_scope_conditions(CycleActor.from_user_payload(user)),
    )
    result = await db.scalars(stmt)
    cycle = result.first()
    if not cycle:
        raise HTTPException(status_code=404, detail="Cycle not found")
    return cycle


async def _validate_target_idea(db: AsyncSession, idea_id: str | None, user: dict[str, Any]) -> None:
    if not idea_id:
        return
    stmt = select(Idea.id).where(
        *target_idea_scope_conditions(idea_id, CycleActor.from_user_payload(user))
    )
    result = await db.execute(stmt)
    if not result.first():
        raise HTTPException(
            status_code=400,
            detail="target_idea_id must belong to the current workspace",
        )


@router.get("", response_model=list[CycleRead], include_in_schema=False)
@router.get("/", response_model=list[CycleRead])
async def list_cycles(
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    stmt = (
        select(Cycle)
        .where(*cycle_scope_conditions(CycleActor.from_user_payload(user)))
        .order_by(Cycle.created_at.desc())
    )
    result = await db.scalars(stmt)
    return [serialize_cycle(cycle) for cycle in result.all()]


@router.post("", response_model=CycleRead, status_code=201, include_in_schema=False)
@router.post("/", response_model=CycleRead, status_code=201)
async def create_cycle(
    body: CycleCreate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    await _validate_target_idea(db, body.target_idea_id, user)
    try:
        cycle = await command_create_cycle(
            db,
            actor=CycleActor.from_user_payload(user),
            name=body.name,
            prompt=body.prompt,
            timezone_name=body.timezone,
            schedule_expr=body.schedule_expr,
            run_at=body.run_at,
            enabled=body.enabled,
            model_override=body.model_override,
            thinking_override=body.thinking_override,
            target_idea_id=body.target_idea_id,
            guidance=body.guidance,
            rationale=body.rationale,
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc
    await db.refresh(cycle)
    payload = serialize_cycle(cycle)
    event = cycle_change_event(cycle)
    await db.commit()
    publish_cycle_change(
        action="create",
        **event,
    )
    return payload


@router.get("/{cycle_id}", response_model=CycleRead)
async def get_cycle(
    cycle_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    return serialize_cycle(await _get_cycle_or_404(db, cycle_id, user))


@router.patch("/{cycle_id}", response_model=CycleRead)
async def update_cycle(
    cycle_id: int,
    body: CycleUpdate,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    cycle = await _get_cycle_or_404(db, cycle_id, user)
    updates = body.model_dump(exclude_unset=True)
    if "target_idea_id" in updates:
        await _validate_target_idea(db, updates["target_idea_id"], user)

    try:
        await command_update_cycle(
            db,
            cycle,
            actor=CycleActor.from_user_payload(user),
            name=updates.get("name"),
            prompt=updates.get("prompt"),
            timezone_name=updates.get("timezone"),
            schedule_expr=updates.get("schedule_expr"),
            run_at=updates.get("run_at", UNSET_CYCLE_FIELD),
            enabled=updates.get("enabled", UNSET_CYCLE_FIELD),
            model_override=updates.get("model_override", UNSET_CYCLE_FIELD),
            thinking_override=updates.get("thinking_override", UNSET_CYCLE_FIELD),
            target_idea_id=updates.get("target_idea_id", UNSET_CYCLE_FIELD),
            guidance=updates.get("guidance"),
            rationale=updates.get("rationale"),
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc
    await db.flush()
    await db.refresh(cycle)
    payload = serialize_cycle(cycle)
    event = cycle_change_event(cycle)
    await db.commit()
    publish_cycle_change(
        action="update",
        **event,
    )
    return payload


@router.delete("/{cycle_id}")
async def delete_cycle(
    cycle_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    cycle = await _get_cycle_or_404(db, cycle_id, user)
    await command_delete_cycle(db, cycle)
    event = cycle_change_event(cycle)
    await db.commit()
    publish_cycle_change(
        action="delete",
        **event,
    )
    return {"ok": True, "id": cycle_id}


@router.get("/{cycle_id}/runs", response_model=list[CycleRunRead])
async def list_cycle_runs(
    cycle_id: int,
    limit: int = 25,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    cycle = await _get_cycle_or_404(db, cycle_id, user)
    stmt = (
        select(CycleRun)
        .where(CycleRun.cycle_id == cycle.id)
        .order_by(CycleRun.created_at.desc())
        .limit(limit)
    )
    result = await db.scalars(stmt)
    return [serialize_cycle_run(run) for run in result.all()]


@router.post("/{cycle_id}/run", response_model=CycleRunRead)
async def run_cycle(
    cycle_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    cycle = await _get_cycle_or_404(db, cycle_id, user)
    event = {
        "org_id": cycle.org_id,
        "user_id": cycle.user_id,
        "cycle_id": cycle_id,
        "target_idea_id": cycle.target_idea_id,
    }
    payload = await async_run_cycle_now(
        cycle_id,
        run_kind=SCHEDULED_DIGEST_RUN_KIND,
    )
    publish_cycle_change(
        action="run",
        **event,
    )
    return payload
