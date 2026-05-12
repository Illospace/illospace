"""Cortex read projections over AgentRun rows."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from brain.systems.runs.cortex.permissions import RunReadScope, run_belongs_to_scope
from brain.platform.db.models.agent_run import (
    AgentRunArtifactRow,
    AgentRunEventRow,
    AgentRunRow,
)
from brain.platform.db.repositories.unit_of_work import UnitOfWork

ACTIVE_STATUSES = frozenset({"queued", "starting", "running", "paused", "verifying"})


def _duration_sec(started_at: Any, completed_at: Any, fallback_end: Any = None) -> int | None:
    start = started_at
    end = completed_at or fallback_end
    if start is None or end is None:
        return None
    try:
        return max(0, int((end - start).total_seconds()))
    except Exception:
        return None


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def project_run_status(status: str | None, fallback: str | None = None) -> str:
    raw = str(status or fallback or "queued").lower()
    if raw in {"queued", "starting", "running", "paused", "verifying", "completed", "failed", "canceled", "expired"}:
        return raw
    if raw in {"cancelled", "superseded"}:
        return "canceled"
    if raw in {"timeout"}:
        return "failed"
    return raw


def run_stream_payload(run: AgentRunRow) -> dict[str, Any]:
    timestamp = _iso(run.created_at)
    return {
        "type": "run",
        "timestamp": timestamp,
        "id": str(run.id),
        "run_id": run.id,
        "idea_id": run.thread_id,
        "thread_id": run.thread_id,
        "org_id": run.org_id,
        "user_id": run.user_id,
        "parent_run_id": run.parent_run_id,
        "root_run_id": run.root_run_id,
        "trace_id": run.trace_id,
        "profile": run.profile,
        "requested_run_profile": run.profile,
        "recipe": run.recipe,
        "status": project_run_status(run.status),
        "message": run.input_message,
        "target_ref": dict(run.target_ref or {}),
        "workspace_ref": dict(run.workspace_ref or {}),
        "model_policy": dict(run.model_policy or {}),
        "metadata": dict(run.metadata_ or {}),
        "created_at": timestamp,
        "updated_at": _iso(run.updated_at),
        "started_at": _iso(run.started_at),
        "paused_at": _iso(run.paused_at),
        "completed_at": _iso(run.completed_at),
        "failed_at": _iso(run.failed_at),
        "canceled_at": _iso(run.canceled_at),
        "duration_sec": _duration_sec(run.started_at, run.completed_at or run.failed_at or run.canceled_at, run.updated_at),
    }


def _visible_query(scope: RunReadScope | None):
    stmt = select(AgentRunRow)
    if scope and not scope.unrestricted:
        stmt = stmt.where(AgentRunRow.org_id == scope.org_id)
    return stmt


def _active_runs_stmt(scope: RunReadScope | None):
    return (
        _visible_query(scope)
        .where(AgentRunRow.status.in_(sorted(ACTIVE_STATUSES)))
        .order_by(AgentRunRow.created_at.asc(), AgentRunRow.id.asc())
    )


def _recent_runs_stmt(scope: RunReadScope | None, *, limit: int):
    return (
        _visible_query(scope)
        .order_by(AgentRunRow.created_at.desc(), AgentRunRow.id.desc())
        .limit(limit)
    )


def _run_history_stmt(idea_id: str):
    return (
        select(AgentRunRow)
        .where(AgentRunRow.thread_id == idea_id)
        .order_by(AgentRunRow.created_at.asc(), AgentRunRow.id.asc())
    )


def _run_debug_events_stmt(run_id: int):
    return (
        select(AgentRunEventRow)
        .where(AgentRunEventRow.run_id == int(run_id))
        .order_by(AgentRunEventRow.sequence_no.asc())
    )


def _run_debug_artifacts_stmt(run_id: int):
    return (
        select(AgentRunArtifactRow)
        .where(AgentRunArtifactRow.run_id == int(run_id))
        .order_by(AgentRunArtifactRow.created_at.asc(), AgentRunArtifactRow.id.asc())
    )


def _debug_payload(
    run: AgentRunRow,
    events: list[AgentRunEventRow],
    artifacts: list[AgentRunArtifactRow],
) -> dict[str, Any]:
    return {
        "run": run_stream_payload(run),
        "events": [
            {
                "id": event.id,
                "sequence_no": event.sequence_no,
                "event_type": event.event_type,
                "payload": event.payload or {},
                "visibility": event.visibility,
                "created_at": _iso(event.created_at),
            }
            for event in events
        ],
        "artifacts": [
            {
                "id": artifact.id,
                "artifact_type": artifact.artifact_type,
                "title": artifact.title,
                "payload": artifact.payload or {},
                "text": artifact.text,
                "uri": artifact.uri,
                "visibility": artifact.visibility,
                "created_at": _iso(artifact.created_at),
            }
            for artifact in artifacts
        ],
    }


def serialize_active_runs(scope: RunReadScope | None = None, *, uow_factory=UnitOfWork) -> list[dict[str, Any]]:
    with uow_factory() as uow:
        rows = uow.session.scalars(_active_runs_stmt(scope)).all()
        return [run_stream_payload(row) for row in rows]


async def serialize_active_runs_async(
    scope: RunReadScope | None = None,
    *,
    uow_factory=UnitOfWork,
) -> list[dict[str, Any]]:
    async with uow_factory() as uow:
        result = await uow.session.scalars(_active_runs_stmt(scope))
        return [run_stream_payload(row) for row in result.all()]


def serialize_recent_runs(
    scope: RunReadScope | None = None,
    *,
    limit: int = 50,
    include_debug: bool = False,
    uow_factory=UnitOfWork,
) -> list[dict[str, Any]]:
    with uow_factory() as uow:
        rows = uow.session.scalars(_recent_runs_stmt(scope, limit=limit)).all()
        payloads = [run_stream_payload(row) for row in rows]
        if include_debug:
            for payload in payloads:
                payload["debug"] = serialize_run_debug(int(payload["run_id"]), scope, uow_factory=uow_factory)
        return payloads


async def serialize_recent_runs_async(
    scope: RunReadScope | None = None,
    *,
    limit: int = 50,
    include_debug: bool = False,
    uow_factory=UnitOfWork,
) -> list[dict[str, Any]]:
    async with uow_factory() as uow:
        result = await uow.session.scalars(_recent_runs_stmt(scope, limit=limit))
        payloads = [run_stream_payload(row) for row in result.all()]
        if include_debug:
            for payload in payloads:
                payload["debug"] = await serialize_run_debug_async(
                    int(payload["run_id"]),
                    scope,
                    uow_factory=uow_factory,
                )
        return payloads


def serialize_run_history(
    idea_id: str,
    *,
    include_debug: bool = False,
    uow_factory=UnitOfWork,
) -> list[dict[str, Any]]:
    with uow_factory() as uow:
        rows = uow.session.scalars(_run_history_stmt(idea_id)).all()
        payloads = [run_stream_payload(row) for row in rows]
    if include_debug:
        for payload in payloads:
            payload["debug"] = serialize_run_debug(int(payload["run_id"]), None, uow_factory=uow_factory)
    return payloads


async def serialize_run_history_async(
    idea_id: str,
    *,
    include_debug: bool = False,
    uow_factory=UnitOfWork,
) -> list[dict[str, Any]]:
    async with uow_factory() as uow:
        result = await uow.session.scalars(_run_history_stmt(idea_id))
        payloads = [run_stream_payload(row) for row in result.all()]
    if include_debug:
        for payload in payloads:
            payload["debug"] = await serialize_run_debug_async(
                int(payload["run_id"]),
                None,
                uow_factory=uow_factory,
            )
    return payloads


def serialize_run_debug(
    run_id: int,
    scope: RunReadScope | None = None,
    *,
    uow_factory=UnitOfWork,
    **_: Any,
) -> dict[str, Any] | None:
    with uow_factory() as uow:
        run = uow.session.get(AgentRunRow, int(run_id))
        if run is None or (scope is not None and not run_belongs_to_scope(uow.session, run, scope)):
            return None
        events = uow.session.scalars(_run_debug_events_stmt(run.id)).all()
        artifacts = uow.session.scalars(_run_debug_artifacts_stmt(run.id)).all()
        return _debug_payload(run, list(events), list(artifacts))


async def serialize_run_debug_async(
    run_id: int,
    scope: RunReadScope | None = None,
    *,
    uow_factory=UnitOfWork,
    **_: Any,
) -> dict[str, Any] | None:
    async with uow_factory() as uow:
        run = await uow.session.get(AgentRunRow, int(run_id))
        if run is None or (scope is not None and not run_belongs_to_scope(uow.session, run, scope)):
            return None
        events_result = await uow.session.scalars(_run_debug_events_stmt(run.id))
        artifacts_result = await uow.session.scalars(_run_debug_artifacts_stmt(run.id))
        events = events_result.all()
        artifacts = artifacts_result.all()
        return _debug_payload(run, list(events), list(artifacts))


def tenant_safe_queue_status(status: dict[str, Any], _scope: RunReadScope | None = None) -> dict[str, Any]:
    return dict(status or {})


__all__ = [
    "RunReadScope",
    "project_run_status",
    "run_belongs_to_scope",
    "run_stream_payload",
    "serialize_active_runs",
    "serialize_active_runs_async",
    "serialize_recent_runs",
    "serialize_recent_runs_async",
    "serialize_run_debug",
    "serialize_run_debug_async",
    "serialize_run_history",
    "serialize_run_history_async",
    "tenant_safe_queue_status",
]
