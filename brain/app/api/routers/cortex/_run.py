"""Cortex AgentRun endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import select

from brain.systems.runs.events import run_event
from brain.systems.runs.status import RunStatus, TERMINAL_RUN_STATUSES, coerce_run_status
from brain.systems.runs.store import AgentRunStore
from brain.app.api.auth import get_current_user
from brain.app.api.authorization import require_org_context
from brain.app.api.routers.cortex._helpers import _caller_is_service_principal
from brain.app.api.routers.cortex._router import router
from brain.systems.runs.cortex import cancel_runs_for_idea, queue_status
from brain.systems.runs.cortex.permissions import RunReadScope, run_belongs_to_scope
from brain.systems.runs.cortex.recording import (
    agent_trace_export_filename,
    build_agent_trace_snapshot,
    build_agent_trace_export_zip,
    build_thread_trace_snapshot,
)
from brain.systems.runs.cortex.read_models import (
    serialize_active_runs,
    serialize_active_runs_async,
    serialize_recent_runs_async,
    serialize_run_debug_async,
    serialize_run_history_async,
    tenant_safe_queue_status,
)
from brain.platform.db.models.agent_run import AgentRunEventRow, AgentRunRow
from brain.platform.db.models.idea import Idea
from brain.platform.db.repositories.unit_of_work import UnitOfWork, run_unit_of_work_task, open_unit_of_work
from brain.platform.db.session_tasks import run_session_task


class RunSteerRequest(BaseModel):
    content: str


class SkillFeedbackRequest(BaseModel):
    feedback: str | None = None
    note: str | None = None


def _run_event_consumer_running() -> bool | None:
    try:
        from brain.app.api.main import _run_event_consumer_running as _is_running
    except Exception:
        return None
    try:
        return _is_running()
    except Exception:
        return None


def _caller_has_worker_run_visibility(user: dict[str, Any] | None) -> bool:
    return bool(_caller_is_service_principal(user))


def _run_read_scope(user: dict[str, Any] | None) -> RunReadScope | None:
    if user is None:
        return None
    if _caller_has_worker_run_visibility(user):
        return RunReadScope.all_orgs()
    return RunReadScope.for_org(require_org_context(user or {}))


def _require_run_for_user(session, run_id: int, user: dict[str, Any] | None) -> AgentRunRow:
    run = session.get(AgentRunRow, int(run_id))
    if run is None:
        raise HTTPException(status_code=404, detail=f"Run #{run_id} not found")
    scope = _run_read_scope(user)
    if scope is None or not run_belongs_to_scope(session, run, scope):
        raise HTTPException(status_code=404, detail=f"Run #{run_id} not found")
    return run


def _cancel_run_with_event(store: AgentRunStore, run_id: int, *, reason: str) -> None:
    row = store.require_run(int(run_id))
    if coerce_run_status(row.status, default=RunStatus.FAILED) in TERMINAL_RUN_STATUSES:
        return
    store.append_event(run_event(int(run_id), "run.canceled", {"reason": reason}, root_run_id=row.root_run_id))
    store.set_status(int(run_id), RunStatus.CANCELED, reason=reason)


def _require_idea_for_run_history(session, idea_id: str, user: dict[str, Any]) -> None:
    scope = _run_read_scope(user)
    if scope is None:
        raise HTTPException(status_code=404, detail="Idea not found")
    if scope.unrestricted:
        return
    idea = session.get(Idea, idea_id)
    if not idea or str(idea.org_id) != str(scope.org_id):
        raise HTTPException(status_code=404, detail="Idea not found")


def _serialize_active_runs(user: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    return serialize_active_runs(_run_read_scope(user), uow_factory=UnitOfWork)


@router.get("/ops/active")
async def ops_active_runs(user: dict[str, Any] = Depends(get_current_user)):
    return await serialize_active_runs_async(_run_read_scope(user), uow_factory=UnitOfWork)


@router.get("/ops/recent")
async def ops_recent_runs(
    limit: int = 30,
    include_debug: bool = False,
    user: dict[str, Any] = Depends(get_current_user),
):
    return await serialize_recent_runs_async(
        _run_read_scope(user),
        limit=limit,
        include_debug=include_debug,
        uow_factory=UnitOfWork,
    )


@router.get("/runs/{run_id}/tools")
async def run_tools(run_id: int, user: dict[str, Any] = Depends(get_current_user)):
    async with UnitOfWork() as uow:
        await run_session_task(uow.session, lambda sync_db: _require_run_for_user(sync_db, run_id, user))
        result = await uow.session.scalars(
            select(AgentRunEventRow)
            .where(
                AgentRunEventRow.run_id == int(run_id),
                AgentRunEventRow.event_type.in_(["run.tool_started", "run.tool_completed", "run.tool_failed"]),
            )
            .order_by(AgentRunEventRow.sequence_no.asc())
        )
        rows = result.all()
        tools = [{"event_type": row.event_type, "payload": row.payload or {}, "created_at": row.created_at.isoformat()} for row in rows]
        return {"tools": tools, "count": len(tools)}


@router.get("/run/status")
def run_status(user: dict[str, Any] = Depends(get_current_user)):
    scope = _run_read_scope(user)
    status = queue_status(
        consumer_running=_run_event_consumer_running(),
        org_id=None if scope is None or scope.unrestricted else scope.org_id,
    )
    return tenant_safe_queue_status(status, scope or RunReadScope())


@router.get("/run/events/status")
async def run_events_status(user: dict[str, Any] = Depends(get_current_user)):
    from brain.systems.runs.event_log import async_run_event_backbone_status
    from brain.app.api.ws.run_events import DEFAULT_CONSUMER_NAME

    async with UnitOfWork() as uow:
        return await async_run_event_backbone_status(
            uow.session,
            DEFAULT_CONSUMER_NAME,
            consumer_running=_run_event_consumer_running(),
        )


@router.get("/run/history/{idea_id}")
async def run_history(
    idea_id: str,
    include_debug: bool = False,
    user: dict[str, Any] = Depends(get_current_user),
):
    async with UnitOfWork() as uow:
        scope = _run_read_scope(user)
        if scope is None:
            raise HTTPException(status_code=404, detail="Idea not found")
        if not scope.unrestricted:
            idea = await uow.session.get(Idea, idea_id)
            if not idea or str(idea.org_id) != str(scope.org_id):
                raise HTTPException(status_code=404, detail="Idea not found")

    return await serialize_run_history_async(idea_id, include_debug=include_debug, uow_factory=UnitOfWork)


@router.get("/run/{run_id}/debug")
async def run_debug(run_id: int, user: dict[str, Any] = Depends(get_current_user)):
    result = await serialize_run_debug_async(
        run_id,
        _run_read_scope(user),
        uow_factory=UnitOfWork,
    )
    if result is None:
        raise HTTPException(status_code=404, detail=f"Run #{run_id} not found")
    return result


@router.post("/run/{run_id}/trace-export.zip")
def download_run_trace_export(run_id: int, user: dict[str, Any] = Depends(get_current_user)):
    with open_unit_of_work(UnitOfWork) as uow:
        run = _require_run_for_user(uow.session, run_id, user)
        snapshot = build_agent_trace_snapshot(
            uow.session,
            run,
            saved_by=str(user.get("id")) if user.get("id") else None,
        )
        archive = build_agent_trace_export_zip(snapshot)
        filename = agent_trace_export_filename(snapshot)
        return Response(
            content=archive,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Trace-Id": str(snapshot.get("trace_id") or ""),
            },
        )


@router.post("/ideas/{idea_id}/trace-export.zip")
def download_thread_trace_export(idea_id: str, user: dict[str, Any] = Depends(get_current_user)):
    with open_unit_of_work(UnitOfWork) as uow:
        _require_idea_for_run_history(uow.session, idea_id, user)
        snapshot = build_thread_trace_snapshot(
            uow.session,
            idea_id,
            saved_by=str(user.get("id")) if user.get("id") else None,
        )
        archive = build_agent_trace_export_zip(snapshot)
        filename = agent_trace_export_filename(snapshot)
        return Response(
            content=archive,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Trace-Id": str(snapshot.get("trace_id") or ""),
            },
        )


@router.post("/run/{run_id}/approve")
async def approve_run(run_id: int, user: dict[str, Any] = Depends(get_current_user)):
    def _approve():
        with open_unit_of_work(UnitOfWork) as uow:
            _require_run_for_user(uow.session, run_id, user)
            AgentRunStore(uow.session).set_status(int(run_id), RunStatus.RUNNING, reason="approved")
            return {"ok": True, "run_id": run_id}

    return await run_unit_of_work_task(_approve)


@router.post("/run/{run_id}/deny")
async def deny_run(run_id: int, user: dict[str, Any] = Depends(get_current_user)):
    def _deny():
        with open_unit_of_work(UnitOfWork) as uow:
            _require_run_for_user(uow.session, run_id, user)
            _cancel_run_with_event(AgentRunStore(uow.session), int(run_id), reason="approval_denied")
            return {"ok": True, "run_id": run_id}

    return await run_unit_of_work_task(_deny)


@router.post("/run/{run_id}/cancel")
async def cancel_run(run_id: int, user: dict[str, Any] = Depends(get_current_user)):
    def _cancel():
        with open_unit_of_work(UnitOfWork) as uow:
            _require_run_for_user(uow.session, run_id, user)
            _cancel_run_with_event(AgentRunStore(uow.session), int(run_id), reason="user_canceled")
            return {"ok": True, "run_id": run_id}

    return await run_unit_of_work_task(_cancel)


@router.post("/run/{run_id}/steer")
async def steer_run(
    run_id: int,
    payload: RunSteerRequest,
    user: dict[str, Any] = Depends(get_current_user),
):
    content = " ".join(str(payload.content or "").split())
    if not content:
        raise HTTPException(status_code=400, detail="Steering content is required")
    def _steer():
        with open_unit_of_work(UnitOfWork) as uow:
            run = _require_run_for_user(uow.session, run_id, user)
            if coerce_run_status(run.status) in TERMINAL_RUN_STATUSES:
                raise HTTPException(status_code=409, detail="Run is no longer active")
            event = AgentRunStore(uow.session).append_steering(
                int(run_id),
                content,
                user_id=str(user.get("id")) if user.get("id") and user.get("id") != "system" else None,
            )
            return {"ok": True, "run_id": run_id, "event_id": event.id}

    return await run_unit_of_work_task(_steer)


@router.get("/run/{run_id}/graph")
async def run_graph(run_id: int, user: dict[str, Any] = Depends(get_current_user)):
    debug = await run_debug(run_id, user)
    return {"run_id": run_id, "steps": [], "events": debug.get("events", [])}


@router.post("/run/{run_id}/skill-feedback")
async def run_skill_feedback(
    run_id: int,
    payload: SkillFeedbackRequest,
    user: dict[str, Any] = Depends(get_current_user),
):
    def _feedback():
        with open_unit_of_work(UnitOfWork) as uow:
            _require_run_for_user(uow.session, run_id, user)
            store = AgentRunStore(uow.session)
            store.append_event(
                run_event(
                    int(run_id),
                    "run.feedback_received",
                    {"feedback": payload.feedback, "note": payload.note, "user_id": user.get("id")},
                )
            )
        return {"ok": True}

    return await run_unit_of_work_task(_feedback)
