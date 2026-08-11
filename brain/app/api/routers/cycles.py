"""Cycles router."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.app.api.auth import get_current_user
from brain.app.api.deps import get_db, rate_limit
from brain.app.api.schemas.cycles import (
    CycleCreate,
    CyclePolicyApplyRead,
    CyclePolicyApplyRequest,
    CyclePolicyHistoryRead,
    CyclePolicyPreviewRead,
    CyclePolicyPreviewRequest,
    CyclePolicyRevertApplyRequest,
    CycleRead,
    CycleRunRead,
    CycleUpdate,
    EffectiveCyclePolicyRead,
)
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
from brain.systems.cycles.behavior_policy import (
    CyclePolicyApplied,
    CyclePolicyApplyResult,
    CyclePolicyConflict,
    CyclePolicyPatch,
    async_apply_cycle_policy_change as command_apply_cycle_policy,
    async_apply_cycle_policy_revert as command_apply_cycle_policy_revert,
    async_list_cycle_policy_history as command_list_cycle_policy_history,
    async_preview_cycle_policy_change as command_preview_cycle_policy,
    async_preview_cycle_policy_revert as command_preview_cycle_policy_revert,
)
from brain.systems.cycles.behavior_policy_read_model import (
    async_read_effective_cycle_policy as query_effective_cycle_policy,
)
from brain.systems.cycles.events import publish_cycle_change_safe
from brain.systems.cycles.common import SCHEDULED_DIGEST_RUN_KIND
from brain.systems.cycles.serializers import (
    serialize_behavior_change_record,
    serialize_cycle,
    serialize_cycle_policy_preview,
    serialize_cycle_run,
    serialize_effective_cycle_policy,
)
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


def _policy_request_error(exc: ValueError) -> HTTPException:
    detail = str(exc)
    if detail in {"Cycle not found", "Behavior change not found"}:
        return HTTPException(status_code=404, detail=detail)
    return _bad_request(exc)


def _policy_patch(body: CyclePolicyPreviewRequest) -> CyclePolicyPatch:
    proposal = body.proposal.model_dump(exclude_unset=True)
    return CyclePolicyPatch(
        prompt=proposal.get("prompt", UNSET_CYCLE_FIELD),
        schedule_expr=proposal.get("schedule_expr", UNSET_CYCLE_FIELD),
        timezone_name=proposal.get("timezone", UNSET_CYCLE_FIELD),
        enabled=proposal.get("enabled", UNSET_CYCLE_FIELD),
        model_override=proposal.get("model_override", UNSET_CYCLE_FIELD),
        thinking_override=proposal.get(
            "thinking_override",
            UNSET_CYCLE_FIELD,
        ),
        guidance=proposal.get("guidance", UNSET_CYCLE_FIELD),
    )


def _policy_source_reference(cycle_id: int) -> str:
    return f"api:/cycles/{cycle_id}/behavior-policy"


async def _latest_effective_policy_payload(
    db: AsyncSession,
    *,
    actor: CycleActor,
    cycle_id: int,
) -> dict:
    latest = await query_effective_cycle_policy(
        db,
        actor=actor,
        cycle_id=cycle_id,
    )
    return serialize_effective_cycle_policy(latest)


async def _raise_policy_conflict(
    db: AsyncSession,
    *,
    actor: CycleActor,
    cycle_id: int,
    conflict: CyclePolicyConflict,
) -> None:
    latest = await _latest_effective_policy_payload(
        db,
        actor=actor,
        cycle_id=cycle_id,
    )
    raise HTTPException(
        status_code=409,
        detail={
            "reason": conflict.reason,
            "latest_effective_policy": latest,
        },
    )


async def _finalize_policy_apply_result(
    db: AsyncSession,
    *,
    actor: CycleActor,
    cycle_id: int,
    result: CyclePolicyApplyResult,
    invalid_result_detail: str,
) -> dict:
    try:
        if isinstance(result, CyclePolicyConflict):
            await _raise_policy_conflict(
                db,
                actor=actor,
                cycle_id=cycle_id,
                conflict=result,
            )
        if not isinstance(result, CyclePolicyApplied):
            raise ValueError(invalid_result_detail)
        effective_policy = await _latest_effective_policy_payload(
            db,
            actor=actor,
            cycle_id=cycle_id,
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise _policy_request_error(exc) from exc
    payload = {
        "effective_policy": effective_policy,
        "change": serialize_behavior_change_record(result.change),
    }
    await db.commit()
    return payload


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
            max_concurrency=body.max_concurrency,
            timeout_seconds=body.timeout_seconds,
            model_override=body.model_override,
            thinking_override=body.thinking_override,
            execution_policy_key=body.execution_policy_key,
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
    publish_cycle_change_safe(
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
            max_concurrency=updates.get("max_concurrency", UNSET_CYCLE_FIELD),
            timeout_seconds=updates.get("timeout_seconds", UNSET_CYCLE_FIELD),
            model_override=updates.get("model_override", UNSET_CYCLE_FIELD),
            thinking_override=updates.get("thinking_override", UNSET_CYCLE_FIELD),
            execution_policy_key=updates.get(
                "execution_policy_key", UNSET_CYCLE_FIELD
            ),
            target_idea_id=updates.get("target_idea_id", UNSET_CYCLE_FIELD),
            guidance=updates.get("guidance"),
            rationale=updates.get("rationale"),
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc
    await db.flush()
    await db.refresh(cycle)
    payload = serialize_cycle(cycle)
    await db.commit()
    return payload


@router.get(
    "/{cycle_id}/behavior-policy",
    response_model=EffectiveCyclePolicyRead,
)
async def get_cycle_behavior_policy(
    cycle_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    actor = CycleActor.from_user_payload(user)
    try:
        return await _latest_effective_policy_payload(
            db,
            actor=actor,
            cycle_id=cycle_id,
        )
    except ValueError as exc:
        raise _policy_request_error(exc) from exc


@router.post(
    "/{cycle_id}/behavior-policy/preview",
    response_model=CyclePolicyPreviewRead,
)
async def preview_cycle_behavior_policy(
    cycle_id: int,
    body: CyclePolicyPreviewRequest,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    try:
        preview = await command_preview_cycle_policy(
            db,
            actor=CycleActor.from_user_payload(user),
            cycle_id=cycle_id,
            proposal=_policy_patch(body),
        )
    except ValueError as exc:
        raise _policy_request_error(exc) from exc
    return serialize_cycle_policy_preview(preview)


@router.post(
    "/{cycle_id}/behavior-policy/apply",
    response_model=CyclePolicyApplyRead,
)
async def apply_cycle_behavior_policy(
    cycle_id: int,
    body: CyclePolicyApplyRequest,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    actor = CycleActor.from_user_payload(user)
    try:
        result = await command_apply_cycle_policy(
            db,
            actor=actor,
            cycle_id=cycle_id,
            proposal=_policy_patch(body),
            expected_version=body.expected_version,
            preview_digest=body.preview_digest,
            rationale=body.rationale,
            source_reference=_policy_source_reference(cycle_id),
        )
    except ValueError as exc:
        raise _policy_request_error(exc) from exc
    return await _finalize_policy_apply_result(
        db,
        actor=actor,
        cycle_id=cycle_id,
        result=result,
        invalid_result_detail="Cycle policy apply returned an invalid result",
    )


@router.get(
    "/{cycle_id}/behavior-policy/history",
    response_model=CyclePolicyHistoryRead,
)
async def list_cycle_behavior_policy_history(
    cycle_id: int,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    try:
        changes = await command_list_cycle_policy_history(
            db,
            actor=CycleActor.from_user_payload(user),
            cycle_id=cycle_id,
            limit=limit + 1,
            offset=offset,
        )
    except ValueError as exc:
        raise _policy_request_error(exc) from exc
    has_more = len(changes) > limit
    page = changes[:limit]
    return {
        "items": [
            serialize_behavior_change_record(change)
            for change in page
        ],
        "pagination": {
            "limit": limit,
            "offset": offset,
            "has_more": has_more,
            "next_offset": offset + len(page) if has_more else None,
        },
    }


@router.post(
    "/{cycle_id}/behavior-policy/history/{change_id}/revert/preview",
    response_model=CyclePolicyPreviewRead,
)
async def preview_cycle_behavior_policy_revert(
    cycle_id: int,
    change_id: int,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    try:
        preview = await command_preview_cycle_policy_revert(
            db,
            actor=CycleActor.from_user_payload(user),
            cycle_id=cycle_id,
            change_id=change_id,
        )
    except ValueError as exc:
        raise _policy_request_error(exc) from exc
    return serialize_cycle_policy_preview(preview)


@router.post(
    "/{cycle_id}/behavior-policy/history/{change_id}/revert/apply",
    response_model=CyclePolicyApplyRead,
)
async def apply_cycle_behavior_policy_revert(
    cycle_id: int,
    change_id: int,
    body: CyclePolicyRevertApplyRequest,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    actor = CycleActor.from_user_payload(user)
    try:
        result = await command_apply_cycle_policy_revert(
            db,
            actor=actor,
            cycle_id=cycle_id,
            change_id=change_id,
            expected_version=body.expected_version,
            preview_digest=body.preview_digest,
            rationale=body.rationale,
            source_reference=_policy_source_reference(cycle_id),
        )
    except ValueError as exc:
        raise _policy_request_error(exc) from exc
    return await _finalize_policy_apply_result(
        db,
        actor=actor,
        cycle_id=cycle_id,
        result=result,
        invalid_result_detail="Cycle policy revert returned an invalid result",
    )


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
    publish_cycle_change_safe(
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
    publish_cycle_change_safe(
        action="run",
        **event,
    )
    return payload
