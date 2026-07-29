"""Cortex read projections over AgentRun rows."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from brain.systems.runs.cortex.permissions import RunReadScope, run_belongs_to_scope
from brain.systems.runs.failures import failure_category_for_error, public_run_failure
from brain.systems.runs.presentation import public_tool_event_payload
from brain.systems.runs.visibility import fetch_visible_run_rows, run_is_headless
from brain.platform.db.models.agent_run import (
    AgentRunArtifactRow,
    AgentRunEventRow,
    AgentRunRow,
)
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.contracts.statuses import OPEN_RUN_STATUS_VALUES, project_run_status_value

ACTIVE_STATUSES = frozenset(OPEN_RUN_STATUS_VALUES)
_FAILURE_STATUSES = frozenset({"failed", "canceled", "expired"})
_FAILURE_DIAGNOSTIC_KEYS = frozenset(
    {
        "details",
        "diagnostic",
        "error",
        "errors",
        "exception",
        "final_answer",
        "message",
        "output",
        "reason",
        "result",
        "result_preview",
        "stack",
        "text",
        "traceback",
    }
)
_FAILURE_CONTAINER_KEYS = frozenset({"failed", "failure", "failures"})


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
    return project_run_status_value(status, fallback)


def _failure_category(run: AgentRunRow, events: list[AgentRunEventRow] | None = None) -> Any:
    metadata = dict(getattr(run, "metadata_", None) or {})
    failure = metadata.get("failure") if isinstance(metadata.get("failure"), dict) else {}
    category = failure.get("category")
    if category:
        return category
    for event in reversed(events or []):
        if str(getattr(event, "event_type", "") or "") != "run.failed":
            continue
        payload = dict(getattr(event, "payload", None) or {})
        category = payload.get("failure_category") or payload.get("category")
        if category:
            return category
    return None


def public_failure_for_run(
    run: AgentRunRow,
    events: list[AgentRunEventRow] | None = None,
) -> dict[str, str] | None:
    status = project_run_status(getattr(run, "status", None))
    if status not in _FAILURE_STATUSES:
        return None
    return public_run_failure(status, _failure_category(run, events))


def _strip_failure_diagnostics(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_failure_diagnostics(item)
            for key, item in value.items()
            if str(key).strip().lower() not in _FAILURE_DIAGNOSTIC_KEYS
        }
    if isinstance(value, list):
        return [_strip_failure_diagnostics(item) for item in value]
    if isinstance(value, tuple):
        return [_strip_failure_diagnostics(item) for item in value]
    return value


def _strip_embedded_failure_diagnostics(value: Any, *, in_failure: bool = False) -> Any:
    if isinstance(value, dict):
        status = str(value.get("status") or "").strip().lower()
        failure_context = in_failure or status in _FAILURE_STATUSES
        projected = {}
        for key, item in value.items():
            normalized_key = str(key).strip().lower()
            if failure_context and normalized_key in _FAILURE_DIAGNOSTIC_KEYS:
                continue
            projected[key] = _strip_embedded_failure_diagnostics(
                item,
                in_failure=failure_context or normalized_key in _FAILURE_CONTAINER_KEYS,
            )
        return projected
    if isinstance(value, (list, tuple)):
        return [
            _strip_embedded_failure_diagnostics(item, in_failure=in_failure)
            for item in value
        ]
    return value


def public_failed_run_artifact(
    artifact: dict[str, Any],
    failure: dict[str, str] | None,
) -> dict[str, Any]:
    """Return an artifact projection that cannot replay failed-run diagnostics."""

    projected = dict(artifact or {})
    artifact_payload = (
        dict(projected.get("payload") or {})
        if isinstance(projected.get("payload"), dict)
        else {}
    )
    stored_failure = (
        artifact_payload.get("failure")
        if isinstance(artifact_payload.get("failure"), dict)
        else {}
    )
    artifact_status = str(
        stored_failure.get("status") or artifact_payload.get("status") or ""
    ).strip().lower()
    if failure is None and artifact_status in _FAILURE_STATUSES:
        category = (
            stored_failure.get("category")
            or artifact_payload.get("failure_category")
            or artifact_payload.get("category")
            or failure_category_for_error(
                artifact_payload.get("error")
                or artifact_payload.get("output")
                or projected.get("text")
            )
        )
        failure = public_run_failure(artifact_status, category)
    if failure is None:
        return projected
    artifact_type = str(projected.get("artifact_type") or "")
    projected["title"] = None
    projected["payload"] = {"failure": dict(failure)}
    projected["text"] = failure["message"] if artifact_type == "final_answer" else None
    projected["uri"] = None
    return projected


def run_id_from_public_message_metadata(metadata: Any) -> int | None:
    if not isinstance(metadata, dict):
        return None
    value = metadata.get("created_by_run_id") or metadata.get("run_id")
    try:
        return int(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def public_run_linked_message(
    content: Any,
    metadata: Any,
    failure: dict[str, str] | None,
) -> tuple[Any, dict[str, Any]]:
    projected_metadata = dict(metadata or {}) if isinstance(metadata, dict) else {}
    if failure is None:
        return content, projected_metadata
    for key in _FAILURE_DIAGNOSTIC_KEYS:
        projected_metadata.pop(key, None)
    projected_metadata["failure"] = dict(failure)
    return failure["message"], projected_metadata


async def public_failures_for_run_ids(
    session: Any,
    run_ids: list[int] | set[int] | tuple[int, ...],
    *,
    thread_id: str | None = None,
    org_id: str | None = None,
) -> dict[int, dict[str, str]]:
    normalized_ids = sorted({int(run_id) for run_id in run_ids if run_id is not None})
    if not normalized_ids:
        return {}
    run_stmt = select(AgentRunRow).where(AgentRunRow.id.in_(normalized_ids))
    if thread_id is not None:
        run_stmt = run_stmt.where(AgentRunRow.thread_id == str(thread_id))
    if org_id is not None:
        run_stmt = run_stmt.where(AgentRunRow.org_id == str(org_id))
    runs = list(
        (
            await session.scalars(run_stmt)
        ).all()
    )
    visible_ids = [int(run.id) for run in runs]
    if not visible_ids:
        return {}
    events = list(
        (
            await session.scalars(
                select(AgentRunEventRow)
                .where(
                    AgentRunEventRow.run_id.in_(visible_ids),
                    AgentRunEventRow.event_type == "run.failed",
                )
                .order_by(AgentRunEventRow.sequence_no.asc(), AgentRunEventRow.id.asc())
            )
        ).all()
    )
    events_by_run: dict[int, list[AgentRunEventRow]] = {}
    for event in events:
        events_by_run.setdefault(int(event.run_id), []).append(event)
    failures = {}
    for run in runs:
        failure = public_failure_for_run(run, events_by_run.get(int(run.id), []))
        if failure is not None:
            failures[int(run.id)] = failure
    return failures


def run_stream_payload(run: AgentRunRow) -> dict[str, Any]:
    timestamp = _iso(run.created_at)
    status = project_run_status(run.status)
    failure = public_failure_for_run(run)
    metadata = _strip_embedded_failure_diagnostics(dict(run.metadata_ or {}))
    if failure is not None:
        metadata.pop("failure", None)
        metadata = _strip_failure_diagnostics(metadata)
    payload = {
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
        "status": status,
        "message": run.input_message,
        "target_ref": (
            _strip_failure_diagnostics(dict(run.target_ref or {}))
            if failure
            else _strip_embedded_failure_diagnostics(dict(run.target_ref or {}))
        ),
        "workspace_ref": (
            _strip_failure_diagnostics(dict(run.workspace_ref or {}))
            if failure
            else _strip_embedded_failure_diagnostics(dict(run.workspace_ref or {}))
        ),
        "model_policy": (
            _strip_failure_diagnostics(dict(run.model_policy or {}))
            if failure
            else _strip_embedded_failure_diagnostics(dict(run.model_policy or {}))
        ),
        "metadata": metadata,
        "created_at": timestamp,
        "updated_at": _iso(run.updated_at),
        "started_at": _iso(run.started_at),
        "deadline_at": _iso(getattr(run, "deadline_at", None)),
        "closeout_expires_at": _iso(getattr(run, "closeout_expires_at", None)),
        "expired_at": _iso(getattr(run, "expired_at", None)),
        "paused_at": _iso(run.paused_at),
        "completed_at": _iso(run.completed_at),
        "failed_at": _iso(run.failed_at),
        "canceled_at": _iso(run.canceled_at),
        "duration_sec": _duration_sec(
            run.started_at,
            run.completed_at
            or run.failed_at
            or run.canceled_at
            or getattr(run, "expired_at", None),
            run.updated_at,
        ),
    }
    if failure is not None:
        payload["failure"] = failure
    return payload


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


def _recent_runs_stmt(scope: RunReadScope | None):
    return (
        _visible_query(scope)
        .order_by(AgentRunRow.created_at.desc(), AgentRunRow.id.desc())
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
    failure = public_failure_for_run(run, events)
    run_payload = run_stream_payload(run)
    if failure is not None:
        run_payload["failure"] = failure
    return {
        "run": run_payload,
        "events": [
            {
                "id": event.id,
                "sequence_no": event.sequence_no,
                "event_type": event.event_type,
                "payload": public_run_debug_event_payload(event, failure),
                "visibility": event.visibility,
                "created_at": _iso(event.created_at),
            }
            for event in events
        ],
        "artifacts": [
            public_failed_run_artifact(
                {
                "id": artifact.id,
                "artifact_type": artifact.artifact_type,
                "title": artifact.title,
                "payload": dict(artifact.payload or {}),
                "text": artifact.text,
                "uri": artifact.uri,
                "visibility": artifact.visibility,
                "created_at": _iso(artifact.created_at),
                },
                failure,
            )
            for artifact in artifacts
        ],
    }


def public_run_debug_event_payload(
    event: AgentRunEventRow,
    failure: dict[str, str] | None,
) -> dict[str, Any]:
    payload = dict(event.payload or {})
    event_type = str(event.event_type or "")
    if event_type in {"run.tool_started", "run.tool_completed", "run.tool_failed"}:
        payload = public_tool_event_payload(payload, event_type)
        projected_failure = payload.get("failure")
        if failure is None and isinstance(projected_failure, dict):
            failure = projected_failure
    if failure is None and (
        "failed" in event_type
        or (
            event_type == "run.status_changed"
            and project_run_status(str(payload.get("to_status") or "")) in _FAILURE_STATUSES
        )
    ):
        stored_failure = payload.get("failure") if isinstance(payload.get("failure"), dict) else {}
        category = (
            stored_failure.get("category")
            or payload.get("failure_category")
            or payload.get("category")
            or failure_category_for_error(
                payload.get("error")
                or payload.get("reason")
                or payload.get("result")
                or payload.get("result_preview")
            )
        )
        failure = public_run_failure("failed", category)
    if failure is None:
        return payload
    if event_type in {"run.failed", "run.canceled", "run.expired"}:
        return {"failure": failure}
    if event_type == "run.tool_failed":
        for key in ("error", "result", "result_preview"):
            payload.pop(key, None)
        payload["failure"] = failure
        return payload
    if event_type == "run.tool_completed" and isinstance(payload.get("failure"), dict):
        for key in ("error", "result", "result_preview"):
            payload.pop(key, None)
        payload["failure"] = failure
        return payload
    if "failed" in event_type:
        payload = _strip_failure_diagnostics(payload)
        payload["failure"] = failure
        return payload
    if (
        event_type == "run.status_changed"
        and project_run_status(str(payload.get("to_status") or "")) in _FAILURE_STATUSES
    ):
        for key in ("reason", "error", "message", "text", "final_answer"):
            payload.pop(key, None)
        payload["failure"] = failure
    elif event_type in {"run.text_delta", "run.text_completed"}:
        for key in ("text", "delta", "error", "message", "final_answer"):
            payload.pop(key, None)
        payload["failure"] = failure
        payload["text"] = failure["message"]
    else:
        payload = _strip_failure_diagnostics(payload)
    return payload


async def serialize_active_runs_async(
    scope: RunReadScope | None = None,
    *,
    uow_factory=UnitOfWork,
) -> list[dict[str, Any]]:
    async with uow_factory() as uow:
        rows = await fetch_visible_run_rows(uow.session, _active_runs_stmt(scope))
        return [run_stream_payload(row) for row in rows]


async def serialize_recent_runs_async(
    scope: RunReadScope | None = None,
    *,
    limit: int = 50,
    include_debug: bool = False,
    uow_factory=UnitOfWork,
) -> list[dict[str, Any]]:
    async with uow_factory() as uow:
        rows = await fetch_visible_run_rows(uow.session, _recent_runs_stmt(scope), limit=limit)
        payloads = [run_stream_payload(row) for row in rows]
        if include_debug:
            for payload in payloads:
                payload["debug"] = await serialize_run_debug_async(
                    int(payload["run_id"]),
                    scope,
                    uow_factory=uow_factory,
                )
        return payloads


async def serialize_run_history_async(
    idea_id: str,
    *,
    include_debug: bool = False,
    uow_factory=UnitOfWork,
) -> list[dict[str, Any]]:
    async with uow_factory() as uow:
        rows = await fetch_visible_run_rows(uow.session, _run_history_stmt(idea_id))
        payloads = [run_stream_payload(row) for row in rows]
    if include_debug:
        for payload in payloads:
            payload["debug"] = await serialize_run_debug_async(
                int(payload["run_id"]),
                None,
                uow_factory=uow_factory,
            )
    return payloads


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
    "public_failed_run_artifact",
    "public_failures_for_run_ids",
    "public_failure_for_run",
    "public_run_linked_message",
    "public_run_debug_event_payload",
    "run_belongs_to_scope",
    "run_id_from_public_message_metadata",
    "run_is_headless",
    "run_stream_payload",
    "serialize_active_runs_async",
    "serialize_recent_runs_async",
    "serialize_run_debug_async",
    "serialize_run_history_async",
    "tenant_safe_queue_status",
]
