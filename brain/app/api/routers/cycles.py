"""Cycles router."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from brain.app.api.auth import get_current_user
from brain.app.api.deps import get_db, rate_limit
from brain.app.api.schemas.cycles import CycleCreate, CycleRead, CycleRunRead, CycleUpdate
from brain.systems.cycles.service import (
    compute_next_run_at,
    cycle_defaults,
    REUSABLE_THREAD_EXECUTION_MODE,
    run_cycle_now,
    serialize_cycle,
    validate_nonempty_trimmed,
    serialize_cycle_run,
    validate_execution_mode,
    validate_schedule_expr,
    validate_thinking_override,
    validate_timezone_name,
)
from brain.platform.db.models.cycle import Cycle, CycleRun
from brain.platform.db.models.idea import Idea
from brain.platform.db.models.org import User

router = APIRouter(
    prefix="/api/cycles",
    tags=["cycles"],
    dependencies=[Depends(rate_limit)],
)


def _is_service_principal(user: dict[str, Any]) -> bool:
    return user.get("principal_type") == "service"


def _cycle_scope_conditions(user: dict[str, Any]) -> list[Any]:
    if _is_service_principal(user):
        return [Cycle.deleted_at.is_(None)]
    org_id = user.get("org_id")
    if org_id:
        org_user_ids = select(User.id).where(User.org_id == str(org_id))
        return [
            or_(
                Cycle.org_id == str(org_id),
                and_(Cycle.org_id.is_(None), Cycle.user_id.in_(org_user_ids)),
            ),
            Cycle.deleted_at.is_(None),
        ]
    return [Cycle.user_id == str(user["id"]), Cycle.deleted_at.is_(None)]


def _target_idea_scope_conditions(idea_id: str, user: dict[str, Any]) -> list[Any]:
    conditions: list[Any] = [Idea.id == idea_id]
    if _is_service_principal(user):
        return conditions
    org_id = user.get("org_id")
    if org_id:
        org_user_ids = select(User.id).where(User.org_id == str(org_id))
        conditions.append(
            or_(
                Idea.org_id == str(org_id),
                and_(Idea.org_id.is_(None), Idea.user_id.in_(org_user_ids)),
            )
        )
    else:
        conditions.append(Idea.user_id == str(user["id"]))
    return conditions


def _get_cycle_or_404(db: Session, cycle_id: int, user: dict[str, Any]) -> Cycle:
    stmt = select(Cycle).where(
        Cycle.id == cycle_id,
        *_cycle_scope_conditions(user),
    )
    cycle = db.scalars(stmt).first()
    if not cycle:
        raise HTTPException(status_code=404, detail="Cycle not found")
    return cycle


def _validate_target_idea(db: Session, idea_id: str | None, user: dict[str, Any]) -> None:
    if not idea_id:
        return
    stmt = select(Idea.id).where(*_target_idea_scope_conditions(idea_id, user))
    if not db.execute(stmt).first():
        raise HTTPException(
            status_code=400,
            detail="target_idea_id must belong to the current workspace",
        )


@router.get("", response_model=list[CycleRead], include_in_schema=False)
@router.get("/", response_model=list[CycleRead])
def list_cycles(
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    stmt = (
        select(Cycle)
        .where(*_cycle_scope_conditions(user))
        .order_by(Cycle.created_at.desc())
    )
    return [serialize_cycle(cycle) for cycle in db.scalars(stmt).all()]


@router.post("", response_model=CycleRead, status_code=201, include_in_schema=False)
@router.post("/", response_model=CycleRead, status_code=201)
def create_cycle(
    body: CycleCreate,
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    schedule_expr = validate_schedule_expr(body.schedule_expr)
    timezone_name = validate_timezone_name(body.timezone)
    execution_mode = validate_execution_mode(body.execution_mode)
    thinking_override = validate_thinking_override(body.thinking_override)
    try:
        name = validate_nonempty_trimmed(body.name, "name")
        prompt = validate_nonempty_trimmed(body.prompt, "prompt")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _validate_target_idea(db, body.target_idea_id, user)
    cycle = Cycle(
        user_id=user["id"],
        org_id=user.get("org_id"),
        name=name,
        prompt=prompt,
        schedule_expr=schedule_expr,
        timezone=timezone_name,
        enabled=body.enabled,
        model_override=(body.model_override or "").strip() or None,
        thinking_override=thinking_override,
        execution_mode=REUSABLE_THREAD_EXECUTION_MODE,
        target_idea_id=body.target_idea_id,
        reopen_archived=cycle_defaults(
            execution_mode=execution_mode,
            reopen_archived=body.reopen_archived,
        ),
        next_run_at=compute_next_run_at(schedule_expr, timezone_name),
    )
    db.add(cycle)
    db.flush()
    return serialize_cycle(cycle)


@router.get("/{cycle_id}", response_model=CycleRead)
def get_cycle(
    cycle_id: int,
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    cycle = _get_cycle_or_404(db, cycle_id, user)
    return serialize_cycle(cycle)


@router.patch("/{cycle_id}", response_model=CycleRead)
def update_cycle(
    cycle_id: int,
    body: CycleUpdate,
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    cycle = _get_cycle_or_404(db, cycle_id, user)
    updates = body.model_dump(exclude_unset=True)
    if "target_idea_id" in updates:
        _validate_target_idea(db, updates["target_idea_id"], user)

    if "name" in updates and updates["name"] is not None:
        try:
            cycle.name = validate_nonempty_trimmed(updates["name"], "name")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if "prompt" in updates and updates["prompt"] is not None:
        try:
            cycle.prompt = validate_nonempty_trimmed(updates["prompt"], "prompt")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    if "schedule_expr" in updates and updates["schedule_expr"] is not None:
        cycle.schedule_expr = validate_schedule_expr(updates["schedule_expr"])
    if "timezone" in updates and updates["timezone"] is not None:
        cycle.timezone = validate_timezone_name(updates["timezone"])
    if "enabled" in updates and updates["enabled"] is not None:
        cycle.enabled = updates["enabled"]
    if "model_override" in updates:
        cycle.model_override = (updates["model_override"] or "").strip() or None
    if "thinking_override" in updates:
        cycle.thinking_override = validate_thinking_override(updates["thinking_override"])
    if "execution_mode" in updates and updates["execution_mode"] is not None:
        cycle.execution_mode = validate_execution_mode(updates["execution_mode"])
    if "target_idea_id" in updates:
        cycle.target_idea_id = updates["target_idea_id"]
    if "reopen_archived" in updates and updates["reopen_archived"] is not None:
        cycle.reopen_archived = updates["reopen_archived"]
    elif "execution_mode" in updates and "reopen_archived" not in updates:
        cycle.reopen_archived = cycle_defaults(
            execution_mode=cycle.execution_mode,
            reopen_archived=None,
        )

    cycle.execution_mode = REUSABLE_THREAD_EXECUTION_MODE
    cycle.reopen_archived = True

    cycle.next_run_at = compute_next_run_at(cycle.schedule_expr, cycle.timezone)
    db.flush()
    return serialize_cycle(cycle)


@router.delete("/{cycle_id}")
def delete_cycle(
    cycle_id: int,
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    cycle = _get_cycle_or_404(db, cycle_id, user)
    cycle.enabled = False
    cycle.deleted_at = datetime.now(timezone.utc)
    db.flush()
    return {"ok": True, "id": cycle_id}


@router.get("/{cycle_id}/runs", response_model=list[CycleRunRead])
def list_cycle_runs(
    cycle_id: int,
    limit: int = 25,
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    cycle = _get_cycle_or_404(db, cycle_id, user)
    stmt = (
        select(CycleRun)
        .where(CycleRun.cycle_id == cycle.id)
        .order_by(CycleRun.created_at.desc())
        .limit(limit)
    )
    return [serialize_cycle_run(run) for run in db.scalars(stmt).all()]


@router.post("/{cycle_id}/run", response_model=CycleRunRead)
def run_cycle(
    cycle_id: int,
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    _get_cycle_or_404(db, cycle_id, user)
    return run_cycle_now(cycle_id)
