"""Run trace/recording projection helpers."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from brain.systems.runs.ids import trace_id_for_run_id
from brain.platform.db.models.agent_run import AgentRunArtifactRow, AgentRunEventRow, AgentRunRow


def build_run_run_summary(session, run_id: int) -> dict[str, Any]:
    run = session.get(AgentRunRow, int(run_id))
    if run is None:
        return {"run_id": run_id, "status": "missing", "trace_id": trace_id_for_run_id(run_id)}
    return {
        "run_id": run.id,
        "trace_id": run.trace_id or trace_id_for_run_id(run.id),
        "status": run.status,
        "profile": run.profile,
        "recipe": run.recipe,
        "thread_id": run.thread_id,
        "created_at": _iso(run.created_at),
        "started_at": _iso(run.started_at),
        "completed_at": _iso(run.completed_at),
        "failed_at": _iso(run.failed_at),
        "canceled_at": _iso(run.canceled_at),
    }


def build_run_flight_recorder(session, run_id: int, *, summary: dict[str, Any] | None = None, **_: Any) -> dict[str, Any]:
    events = session.scalars(
        select(AgentRunEventRow)
        .where(AgentRunEventRow.run_id == int(run_id))
        .order_by(AgentRunEventRow.sequence_no.asc())
    ).all()
    artifacts = session.scalars(
        select(AgentRunArtifactRow)
        .where(AgentRunArtifactRow.run_id == int(run_id))
        .order_by(AgentRunArtifactRow.created_at.asc(), AgentRunArtifactRow.id.asc())
    ).all()
    artifact_payloads = [
        artifact.payload
        for artifact in artifacts
        if isinstance(artifact.payload, dict)
    ]
    worker_recordings = _worker_recordings({"run_artifacts": artifact_payloads})
    return {
        "schema_version": 1,
        "summary": summary or build_run_run_summary(session, run_id),
        "events": [
            {
                "id": event.id,
                "sequence_no": event.sequence_no,
                "event_type": event.event_type,
                "payload": event.payload or {},
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
                "created_at": _iso(artifact.created_at),
            }
            for artifact in artifacts
        ],
        "workers": worker_recordings,
    }


def load_run_recordings(session, run_id: int) -> dict[str, Any]:
    summary = build_run_run_summary(session, run_id)
    return {
        "summary": summary,
        "flight_recorder": build_run_flight_recorder(session, run_id, summary=summary),
    }


def persist_run_recordings(run_id: int) -> dict[str, Any] | None:
    return {"run_id": int(run_id), "persisted": False, "reason": "recordings_are_projected_from_events"}


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _safe_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return str(value)


def _get_payload(source: Any, name: str, default: Any = None) -> Any:
    if isinstance(source, dict):
        return source.get(name, default)
    return getattr(source, name, default)


def _worker_recordings(run: Any) -> dict[str, Any]:
    """Project worker assignments/results from canonical run artifacts."""
    run_artifacts = [
        artifact
        for artifact in _safe_list(_get_payload(run, "run_artifacts", []))
        if isinstance(artifact, dict)
    ]
    workers_used = [
        worker
        for worker in _safe_list(_get_payload(run, "workers_used", []))
        if isinstance(worker, dict)
    ]
    assignments = [
        artifact for artifact in run_artifacts
        if artifact.get("type") == "worker_assignment"
    ]
    results = [
        artifact for artifact in run_artifacts
        if artifact.get("type") == "worker_result"
    ]
    synthesis_checks = [
        artifact for artifact in run_artifacts
        if artifact.get("type") == "coordinator_synthesis_check"
    ]
    assignment_by_execution_id = {
        artifact.get("execution_id"): artifact
        for artifact in assignments
        if artifact.get("execution_id")
    }
    worker_rows: list[dict[str, Any]] = []
    seen_execution_ids: set[str] = set()
    for result in results:
        execution_id = result.get("execution_id")
        assignment = assignment_by_execution_id.get(execution_id) or {}
        evidence = result.get("evidence") if isinstance(result.get("evidence"), dict) else {}
        worker_rows.append({
            "worker_id": result.get("worker_id") or assignment.get("worker_id"),
            "execution_id": execution_id,
            "run_id": result.get("run_id") or assignment.get("run_id"),
            "session_id": result.get("session_id") or assignment.get("session_id"),
            "node_id": result.get("node_id") or assignment.get("node_id"),
            "skill": result.get("skill") or assignment.get("skill"),
            "task": assignment.get("task"),
            "assignment": assignment.get("assignment"),
            "ownership_scope": assignment.get("ownership_scope") or {},
            "status": result.get("status"),
            "trust": {
                "status": result.get("trust_status"),
                "reasons": list(result.get("trust_reasons") or []),
                "schema_valid": bool((evidence.get("schema") or {}).get("valid")),
                "unresolved_uncertainty": list(evidence.get("unresolved_uncertainty") or []),
            },
            "evidence": {
                "summary": evidence.get("summary"),
                "files": list(evidence.get("files") or []),
                "commands": list(evidence.get("commands") or []),
                "artifacts": list(evidence.get("artifacts") or []),
                "unresolved_uncertainty": list(evidence.get("unresolved_uncertainty") or []),
            },
            "usage": {
                "tokens": result.get("tokens") or {},
                "estimated_cost": result.get("estimated_cost"),
                "model": result.get("model"),
                "duration_sec": result.get("duration_sec"),
            },
            "error": result.get("error"),
        })
        if execution_id:
            seen_execution_ids.add(execution_id)

    for assignment in assignments:
        execution_id = assignment.get("execution_id")
        if execution_id in seen_execution_ids:
            continue
        worker_rows.append({
            "worker_id": assignment.get("worker_id"),
            "execution_id": execution_id,
            "run_id": assignment.get("run_id"),
            "session_id": assignment.get("session_id"),
            "node_id": assignment.get("node_id"),
            "skill": assignment.get("skill"),
            "task": assignment.get("task"),
            "assignment": assignment.get("assignment"),
            "ownership_scope": assignment.get("ownership_scope") or {},
            "status": "assignment_issued_no_result",
            "trust": {
                "status": "untrusted",
                "reasons": ["missing_worker_result"],
                "schema_valid": False,
                "unresolved_uncertainty": ["Worker assignment was issued but no result artifact was recorded."],
            },
            "evidence": {
                "summary": None,
                "files": [],
                "commands": [],
                "artifacts": [],
                "unresolved_uncertainty": ["Worker assignment was issued but no result artifact was recorded."],
            },
            "usage": {
                "tokens": {},
                "estimated_cost": None,
                "model": None,
                "duration_sec": None,
            },
            "error": "missing_worker_result",
        })

    return {
        "workers": _jsonable(worker_rows),
        "workers_used": _jsonable(workers_used),
        "assignments": _jsonable(assignments),
        "results": _jsonable(results),
        "synthesis_checks": _jsonable(synthesis_checks),
        "worker_count": len(worker_rows),
        "assignment_count": len(assignments),
        "result_count": len(results),
        "trusted_count": sum(1 for row in worker_rows if (row.get("trust") or {}).get("status") in {"trusted", "trusted_with_uncertainty"}),
        "untrusted_count": sum(1 for row in worker_rows if (row.get("trust") or {}).get("status") == "untrusted"),
    }


def trace_id_for_scheduler_run_id(run_id: int | str | None) -> str | None:
    return None if run_id is None else f"scheduler-run:{run_id}"


__all__ = [
    "build_run_flight_recorder",
    "build_run_run_summary",
    "load_run_recordings",
    "persist_run_recordings",
    "trace_id_for_run_id",
    "trace_id_for_scheduler_run_id",
    "_worker_recordings",
]
