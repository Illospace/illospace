"""Run trace/recording projection helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
import json
from pathlib import PurePath
import re
from typing import Any
import zipfile

from sqlalchemy import or_, select

from brain.systems.runs.ids import trace_id_for_run_id
from brain.platform.db.models.agent_run import AgentRunArtifactRow, AgentRunEventRow, AgentRunRow
from brain.platform.db.models.cycle import Cycle, CycleRun
from brain.platform.db.models.idea import IdeaThread


async def build_run_run_summary_async(session, run_id: int) -> dict[str, Any]:
    run = await session.get(AgentRunRow, int(run_id))
    return _run_summary(run, run_id)


def _run_summary(run: AgentRunRow | None, run_id: int) -> dict[str, Any]:
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


async def build_run_flight_recorder_async(
    session,
    run_id: int,
    *,
    summary: dict[str, Any] | None = None,
    **_: Any,
) -> dict[str, Any]:
    events_result = await session.scalars(
        select(AgentRunEventRow)
        .where(AgentRunEventRow.run_id == int(run_id))
        .order_by(AgentRunEventRow.sequence_no.asc())
    )
    artifacts_result = await session.scalars(
        select(AgentRunArtifactRow)
        .where(AgentRunArtifactRow.run_id == int(run_id))
        .order_by(AgentRunArtifactRow.created_at.asc(), AgentRunArtifactRow.id.asc())
    )
    events = events_result.all()
    artifacts = artifacts_result.all()
    artifact_payloads = [
        artifact.payload
        for artifact in artifacts
        if isinstance(artifact.payload, dict)
    ]
    worker_recordings = _worker_recordings({"run_artifacts": artifact_payloads})
    return {
        "schema_version": 1,
        "summary": summary or await build_run_run_summary_async(session, run_id),
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


async def load_run_recordings_async(session, run_id: int) -> dict[str, Any]:
    summary = await build_run_run_summary_async(session, run_id)
    return {
        "summary": summary,
        "flight_recorder": await build_run_flight_recorder_async(session, run_id, summary=summary),
    }


AGENT_TRACE_SNAPSHOT_ARTIFACT_TYPE = "agent_trace_snapshot"
AGENT_TRACE_SNAPSHOT_SCHEMA_VERSION = 1
AGENT_TRACE_EXPORT_SCHEMA_VERSION = 1
TRACE_MAX_MESSAGES = 40
TRACE_MAX_EVENTS = 300
TRACE_MAX_ARTIFACTS = 60
THREAD_TRACE_MAX_MESSAGES = None
THREAD_TRACE_MAX_RUNS = 100
THREAD_TRACE_MAX_EVENTS = 1200
THREAD_TRACE_MAX_ARTIFACTS = 240
TRACE_MAX_CYCLES = 50
TRACE_MAX_CYCLE_RUNS = 200
TRACE_MAX_STRING_CHARS = 4000
TRACE_MAX_COLLECTION_ITEMS = 80
TRACE_MAX_DEPTH = 6
TRACE_REDACTED_VALUE = "[redacted]"
TRACE_REDACTED_SECRET = "[secret redacted]"
TRACE_SECRET_KEY_RE = re.compile(
    r"(^|[_.-])(api[_.-]?key|authorization|credential|credentials|password|private[_.-]?key|secret|token)([_.-]|$)",
    re.IGNORECASE,
)
TRACE_SECRET_VALUE_PATTERNS = [
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"gh[opusr]_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9][A-Za-z0-9_-]{20,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{20,}"),
]


async def build_agent_trace_snapshot_async(
    session,
    run: AgentRunRow,
    *,
    saved_by: str | None = None,
    max_messages: int = TRACE_MAX_MESSAGES,
    max_events: int = TRACE_MAX_EVENTS,
    max_artifacts: int = TRACE_MAX_ARTIFACTS,
) -> dict[str, Any]:
    """Build a bounded, JSON-safe trace bundle using an async DB session."""

    related_run_ids = await _related_run_ids_async(session, run)
    related_runs = await _runs_by_id_async(session, related_run_ids)
    if not related_runs:
        related_runs = [run]
    messages = await _trace_messages_async(session, run, max_messages=max_messages)
    events = await _trace_events_async(session, related_run_ids, max_events=max_events)
    artifacts = await _trace_artifacts_async(session, related_run_ids, max_artifacts=max_artifacts)
    cycle_state = await _trace_cycle_state_async(
        session,
        idea_id=str(run.thread_id) if getattr(run, "thread_id", None) else None,
        runs=related_runs,
    )
    diagnostics = _trace_diagnostics(related_runs, events, artifacts, cycle_state)
    return _agent_trace_snapshot_payload(
        run,
        related_run_ids=related_run_ids,
        messages=messages,
        events=events,
        artifacts=artifacts,
        cycle_state=cycle_state,
        diagnostics=diagnostics,
        saved_by=saved_by,
        max_messages=max_messages,
        max_events=max_events,
        max_artifacts=max_artifacts,
    )


def _agent_trace_snapshot_payload(
    run: AgentRunRow,
    *,
    related_run_ids: list[int],
    messages: list[dict[str, Any]],
    events: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    cycle_state: dict[str, Any],
    diagnostics: dict[str, Any],
    saved_by: str | None,
    max_messages: int,
    max_events: int,
    max_artifacts: int,
) -> dict[str, Any]:
    trace_id = run.trace_id or trace_id_for_run_id(run.id)
    bundle = {
        "schema_version": AGENT_TRACE_SNAPSHOT_SCHEMA_VERSION,
        "trace_id": trace_id,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "saved_by": saved_by,
        "storage_policy": {
            "mode": "bounded_snapshot",
            "max_messages": max_messages,
            "max_events": max_events,
            "max_artifacts": max_artifacts,
            "max_string_chars": TRACE_MAX_STRING_CHARS,
            "large_values": "truncated_in_place",
        },
        "run": _cap_jsonable({
            **_run_summary(run, int(run.id)),
            "parent_run_id": getattr(run, "parent_run_id", None),
            "root_run_id": getattr(run, "root_run_id", None),
            "input_message": getattr(run, "input_message", None),
            "target_ref": getattr(run, "target_ref", None) or {},
            "workspace_ref": getattr(run, "workspace_ref", None) or {},
            "model_policy": getattr(run, "model_policy", None) or {},
            "context_summary": getattr(run, "context_summary", None),
            "metadata": getattr(run, "metadata_", None) or {},
        }),
        "thread": {
            "idea_id": getattr(run, "thread_id", None),
            "messages": messages,
            "selected_message_count": len(messages),
            "message_limit": max_messages,
        },
        "related_run_ids": related_run_ids,
        "events": events,
        "event_count": len(events),
        "event_limit": max_events,
        "tools": _tool_trace(events),
        "artifacts": artifacts,
        "artifact_count": len(artifacts),
        "artifact_limit": max_artifacts,
        "cycles": cycle_state,
        "diagnostics": diagnostics,
    }
    bundle = _redact_trace_secrets(bundle)
    bundle["storage_estimate"] = {
        "json_bytes": len(json.dumps(_jsonable(bundle), sort_keys=True, default=str).encode("utf-8")),
        "truncated": _contains_truncation(bundle),
    }
    return _jsonable(bundle)


async def build_thread_trace_snapshot_async(
    session,
    idea_id: str,
    *,
    saved_by: str | None = None,
    max_messages: int | None = THREAD_TRACE_MAX_MESSAGES,
    max_runs: int = THREAD_TRACE_MAX_RUNS,
    max_events: int = THREAD_TRACE_MAX_EVENTS,
    max_artifacts: int = THREAD_TRACE_MAX_ARTIFACTS,
) -> dict[str, Any]:
    """Build a bounded thread trace export using an async DB session."""

    runs = await _thread_runs_async(session, idea_id, max_runs=max_runs)
    run_ids = [int(run.id) for run in runs if _is_int_like(getattr(run, "id", None))]
    messages = await _trace_thread_messages_async(session, idea_id, max_messages=max_messages)
    events = await _trace_events_async(session, run_ids, max_events=max_events)
    artifacts = await _trace_artifacts_async(session, run_ids, max_artifacts=max_artifacts)
    cycle_state = await _trace_cycle_state_async(session, idea_id=idea_id, runs=runs)
    diagnostics = _trace_diagnostics(runs, events, artifacts, cycle_state)
    return _thread_trace_snapshot_payload(
        idea_id,
        runs=runs,
        run_ids=run_ids,
        messages=messages,
        events=events,
        artifacts=artifacts,
        cycle_state=cycle_state,
        diagnostics=diagnostics,
        saved_by=saved_by,
        max_messages=max_messages,
        max_runs=max_runs,
        max_events=max_events,
        max_artifacts=max_artifacts,
    )


def _thread_trace_snapshot_payload(
    idea_id: str,
    *,
    runs: list[AgentRunRow],
    run_ids: list[int],
    messages: list[dict[str, Any]],
    events: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    cycle_state: dict[str, Any],
    diagnostics: dict[str, Any],
    saved_by: str | None,
    max_messages: int | None,
    max_runs: int,
    max_events: int,
    max_artifacts: int,
) -> dict[str, Any]:
    trace_id = f"thread:{idea_id}"
    bundle = {
        "schema_version": AGENT_TRACE_SNAPSHOT_SCHEMA_VERSION,
        "export_scope": "thread",
        "trace_id": trace_id,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "saved_by": saved_by,
        "storage_policy": {
            "mode": "bounded_thread_snapshot",
            "max_messages": max_messages,
            "messages": "all_thread_messages" if max_messages is None else "latest_thread_messages",
            "max_runs": max_runs,
            "max_events": max_events,
            "max_artifacts": max_artifacts,
            "max_string_chars": TRACE_MAX_STRING_CHARS,
            "large_values": "truncated_in_place",
        },
        "thread": {
            "idea_id": idea_id,
            "messages": messages,
            "selected_message_count": len(messages),
            "message_limit": max_messages,
        },
        "runs": [
            _cap_jsonable({
                **_run_summary(run, int(run.id)),
                "parent_run_id": getattr(run, "parent_run_id", None),
                "root_run_id": getattr(run, "root_run_id", None),
                "input_message": getattr(run, "input_message", None),
                "target_ref": getattr(run, "target_ref", None) or {},
                "workspace_ref": getattr(run, "workspace_ref", None) or {},
                "model_policy": getattr(run, "model_policy", None) or {},
                "context_summary": getattr(run, "context_summary", None),
                "metadata": getattr(run, "metadata_", None) or {},
            })
            for run in runs
        ],
        "run_count": len(runs),
        "run_limit": max_runs,
        "related_run_ids": run_ids,
        "events": events,
        "event_count": len(events),
        "event_limit": max_events,
        "tools": _tool_trace(events),
        "artifacts": artifacts,
        "artifact_count": len(artifacts),
        "artifact_limit": max_artifacts,
        "cycles": cycle_state,
        "diagnostics": diagnostics,
    }
    bundle = _redact_trace_secrets(bundle)
    bundle["storage_estimate"] = {
        "json_bytes": len(json.dumps(_jsonable(bundle), sort_keys=True, default=str).encode("utf-8")),
        "truncated": _contains_truncation(bundle),
    }
    return _jsonable(bundle)


def build_agent_trace_export_zip(
    snapshot: dict[str, Any],
) -> bytes:
    """Package a trace snapshot as a small shareable zip."""

    trace = _redact_trace_secrets(_jsonable(snapshot))
    run = trace.get("run") if isinstance(trace.get("run"), dict) else {}
    thread = trace.get("thread") if isinstance(trace.get("thread"), dict) else {}
    is_thread_export = trace.get("export_scope") == "thread"
    manifest = _jsonable({
        "schema_version": AGENT_TRACE_EXPORT_SCHEMA_VERSION,
        "export_type": "illo_thread_trace" if is_thread_export else "illo_agent_trace",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trace_id": trace.get("trace_id"),
        "run_id": run.get("run_id"),
        "idea_id": thread.get("idea_id"),
        "diagnostics": {
            "cycle_count": ((trace.get("cycles") or {}).get("cycle_count") if isinstance(trace.get("cycles"), dict) else None),
            "cycle_run_count": ((trace.get("cycles") or {}).get("cycle_run_count") if isinstance(trace.get("cycles"), dict) else None),
            "delivery_signal_count": (
                len(((trace.get("diagnostics") or {}).get("delivery_signals") or []))
                if isinstance(trace.get("diagnostics"), dict)
                else None
            ),
        },
        "files": [
            {"path": "trace.json", "description": "Bounded JSON trace for agent analysis."},
            {"path": "activity.json", "description": "Compact chronological activity list derived from the trace."},
            {"path": "manifest.json", "description": "Export metadata."},
            {"path": "README.md", "description": "Human-readable notes about the export."},
        ],
        "storage_estimate": trace.get("storage_estimate") or {},
    })
    activity = _trace_activity_export(trace)
    readme = _trace_export_readme(trace)

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
        archive.writestr("trace.json", json.dumps(trace, indent=2, sort_keys=True))
        archive.writestr("activity.json", json.dumps(activity, indent=2, sort_keys=True))
        archive.writestr("README.md", readme)
    return buffer.getvalue()


def agent_trace_export_filename(snapshot: dict[str, Any]) -> str:
    if snapshot.get("export_scope") == "thread":
        thread = snapshot.get("thread") if isinstance(snapshot.get("thread"), dict) else {}
        idea_id = thread.get("idea_id") or snapshot.get("trace_id") or "unknown"
        safe_idea_id = _safe_filename_part(idea_id)
        return f"illo-thread-trace-{safe_idea_id or 'unknown'}.zip"
    run = snapshot.get("run") if isinstance(snapshot.get("run"), dict) else {}
    run_id = run.get("run_id") or snapshot.get("trace_id") or "unknown"
    safe_run_id = _safe_filename_part(run_id)
    return f"illo-trace-run-{safe_run_id or 'unknown'}.zip"


def persist_run_recordings(run_id: int) -> dict[str, Any] | None:
    return {"run_id": int(run_id), "persisted": False, "reason": "recordings_are_projected_from_events"}


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


async def _related_run_ids_async(session, run: AgentRunRow) -> list[int]:
    root_run_id = getattr(run, "root_run_id", None) or getattr(run, "id", None)
    result = await session.scalars(
        select(AgentRunRow.id)
        .where((AgentRunRow.id == int(root_run_id)) | (AgentRunRow.root_run_id == int(root_run_id)))
        .order_by(AgentRunRow.created_at.asc(), AgentRunRow.id.asc())
    )
    ids = _coerce_run_ids(result.all())
    if int(run.id) not in ids:
        ids.insert(0, int(run.id))
    return ids


def _coerce_run_ids(rows: list[Any]) -> list[int]:
    ids: list[int] = []
    for value in rows:
        candidate = getattr(value, "id", value)
        try:
            ids.append(int(candidate))
        except (TypeError, ValueError):
            continue
    return ids


async def _thread_runs_async(session, idea_id: str, *, max_runs: int) -> list[AgentRunRow]:
    result = await session.scalars(
        select(AgentRunRow)
        .where(AgentRunRow.thread_id == str(idea_id))
        .order_by(AgentRunRow.created_at.asc(), AgentRunRow.id.asc())
        .limit(max_runs)
    )
    return list(result.all())


async def _trace_messages_async(session, run: AgentRunRow, *, max_messages: int) -> list[dict[str, Any]]:
    return await _trace_thread_messages_async(session, str(run.thread_id), max_messages=max_messages)


async def _runs_by_id_async(session, run_ids: list[int]) -> list[AgentRunRow]:
    if not run_ids:
        return []
    result = await session.scalars(
        select(AgentRunRow)
        .where(AgentRunRow.id.in_(run_ids))
        .order_by(AgentRunRow.created_at.asc(), AgentRunRow.id.asc())
    )
    return list(result.all())


async def _trace_thread_messages_async(
    session,
    idea_id: str,
    *,
    max_messages: int | None,
) -> list[dict[str, Any]]:
    query = select(IdeaThread).where(IdeaThread.idea_id == str(idea_id))
    if max_messages is None:
        result = await session.scalars(query.order_by(IdeaThread.created_at.asc(), IdeaThread.id.asc()))
        rows = result.all()
    else:
        result = await session.scalars(
            query
            .order_by(IdeaThread.created_at.desc(), IdeaThread.id.desc())
            .limit(max_messages)
        )
        rows = list(reversed(result.all()))
    return _thread_message_payloads(rows)


def _thread_message_payloads(rows: list[IdeaThread]) -> list[dict[str, Any]]:
    messages = []
    for row in rows:
        metadata = row.metadata_ if isinstance(row.metadata_, dict) else {}
        messages.append(_cap_jsonable({
            "id": row.id,
            "role": row.role,
            "created_at": _iso(row.created_at),
            "message_type": row.message_type,
            "content": row.content,
            "attachments_count": len(row.attachments or []),
            "metadata": {
                "run_id": metadata.get("run_id"),
                "trace_id": metadata.get("trace_id"),
                "execution_profile": metadata.get("execution_profile"),
                "live_agent_text": metadata.get("live_agent_text"),
            },
        }))
    return messages


async def _trace_events_async(session, run_ids: list[int], *, max_events: int) -> list[dict[str, Any]]:
    if not run_ids:
        return []
    per_run_limit = max(10, max_events // max(len(run_ids), 1))
    rows: list[Any] = []
    for run_id in run_ids:
        result = await session.scalars(
            select(AgentRunEventRow)
            .where(AgentRunEventRow.run_id == int(run_id))
            .order_by(AgentRunEventRow.sequence_no.asc(), AgentRunEventRow.id.asc())
            .limit(per_run_limit)
        )
        rows.extend(result.all())
    return [
        _cap_jsonable({
            "id": event.id,
            "run_id": event.run_id,
            "root_run_id": event.root_run_id,
            "sequence_no": event.sequence_no,
            "event_type": event.event_type,
            "visibility": event.visibility,
            "producer": event.producer,
            "created_at": _iso(event.created_at),
            "payload": event.payload or {},
        })
        for event in rows
    ]


async def _trace_artifacts_async(session, run_ids: list[int], *, max_artifacts: int) -> list[dict[str, Any]]:
    if not run_ids:
        return []
    per_run_limit = max(5, max_artifacts // max(len(run_ids), 1))
    rows: list[Any] = []
    for run_id in run_ids:
        result = await session.scalars(
            select(AgentRunArtifactRow)
            .where(
                AgentRunArtifactRow.run_id == int(run_id),
                AgentRunArtifactRow.artifact_type != AGENT_TRACE_SNAPSHOT_ARTIFACT_TYPE,
            )
            .order_by(AgentRunArtifactRow.created_at.desc(), AgentRunArtifactRow.id.desc())
            .limit(per_run_limit)
        )
        run_rows = result.all()
        rows.extend(reversed(run_rows))
    return [
        _cap_jsonable({
            "id": artifact.id,
            "run_id": artifact.run_id,
            "root_run_id": artifact.root_run_id,
            "artifact_type": artifact.artifact_type,
            "title": artifact.title,
            "uri": artifact.uri,
            "visibility": artifact.visibility,
            "created_at": _iso(artifact.created_at),
            "text": artifact.text,
            "payload": artifact.payload or {},
        })
        for artifact in rows
    ]


async def _trace_cycle_state_async(
    session,
    *,
    idea_id: str | None,
    runs: list[AgentRunRow],
    max_cycles: int = TRACE_MAX_CYCLES,
    max_cycle_runs: int = TRACE_MAX_CYCLE_RUNS,
) -> dict[str, Any]:
    """Include Cycle control-plane rows that explain scheduled-run lifecycle state."""

    run_ids = [
        int(run.id)
        for run in runs
        if _is_int_like(getattr(run, "id", None))
    ]
    cycle_ids: set[int] = set()
    cycle_run_ids: set[int] = set()
    for run in runs:
        metadata = getattr(run, "metadata_", None)
        if not isinstance(metadata, dict):
            continue
        _add_int(cycle_ids, metadata.get("cycle_id"))
        _add_int(cycle_run_ids, metadata.get("cycle_run_id"))
        envelope = metadata.get("launch_envelope")
        if isinstance(envelope, dict):
            _add_int(cycle_ids, envelope.get("cycle_id"))
            _add_int(cycle_run_ids, envelope.get("cycle_run_id"))

    try:
        cycle_rows = await _query_cycles_async(session, idea_id=idea_id, cycle_ids=cycle_ids, limit=max_cycles)
        for cycle in cycle_rows:
            _add_int(cycle_ids, getattr(cycle, "id", None))

        cycle_run_rows = await _query_cycle_runs_async(
            session,
            idea_id=idea_id,
            run_ids=run_ids,
            cycle_ids=cycle_ids,
            cycle_run_ids=cycle_run_ids,
            limit=max_cycle_runs,
        )
        for cycle_run in cycle_run_rows:
            _add_int(cycle_ids, getattr(cycle_run, "cycle_id", None))
            _add_int(cycle_run_ids, getattr(cycle_run, "id", None))

        missing_cycle_ids = {
            cycle_id
            for cycle_id in cycle_ids
            if cycle_id not in {int(cycle.id) for cycle in cycle_rows if _is_int_like(getattr(cycle, "id", None))}
        }
        if missing_cycle_ids:
            cycle_rows.extend(await _query_cycles_async(session, idea_id=None, cycle_ids=missing_cycle_ids, limit=max_cycles))
    except Exception as exc:
        return {
            "schema_version": 1,
            "cycles": [],
            "cycle_runs": [],
            "cycle_ids": sorted(cycle_ids),
            "cycle_run_ids": sorted(cycle_run_ids),
            "error": str(exc),
        }

    cycles = [_cap_jsonable(_cycle_payload(cycle)) for cycle in cycle_rows]
    cycle_runs = [_cap_jsonable(_cycle_run_payload(cycle_run)) for cycle_run in cycle_run_rows]
    return {
        "schema_version": 1,
        "cycles": cycles,
        "cycle_runs": cycle_runs,
        "cycle_count": len(cycles),
        "cycle_run_count": len(cycle_runs),
        "cycle_ids": sorted({int(cycle["id"]) for cycle in cycles if _is_int_like(cycle.get("id"))}),
        "cycle_run_ids": sorted({int(run["id"]) for run in cycle_runs if _is_int_like(run.get("id"))}),
    }


async def _query_cycles_async(
    session,
    *,
    idea_id: str | None,
    cycle_ids: set[int],
    limit: int,
) -> list[Cycle]:
    conditions = []
    if idea_id:
        conditions.append(Cycle.target_idea_id == str(idea_id))
    if cycle_ids:
        conditions.append(Cycle.id.in_(sorted(cycle_ids)))
    if not conditions:
        return []
    result = await session.scalars(
        select(Cycle)
        .where(or_(*conditions))
        .order_by(Cycle.updated_at.desc(), Cycle.id.asc())
        .limit(limit)
    )
    return list(result.all())


async def _query_cycle_runs_async(
    session,
    *,
    idea_id: str | None,
    run_ids: list[int],
    cycle_ids: set[int],
    cycle_run_ids: set[int],
    limit: int,
) -> list[CycleRun]:
    conditions = []
    if idea_id:
        conditions.append(CycleRun.idea_id == str(idea_id))
    if run_ids:
        conditions.append(CycleRun.run_id.in_(run_ids))
    if cycle_ids:
        conditions.append(CycleRun.cycle_id.in_(sorted(cycle_ids)))
    if cycle_run_ids:
        conditions.append(CycleRun.id.in_(sorted(cycle_run_ids)))
    if not conditions:
        return []
    result = await session.scalars(
        select(CycleRun)
        .where(or_(*conditions))
        .order_by(CycleRun.created_at.desc(), CycleRun.id.desc())
        .limit(limit)
    )
    rows = result.all()
    return list(reversed(rows))


def _cycle_payload(cycle: Cycle) -> dict[str, Any]:
    return {
        "id": cycle.id,
        "user_id": cycle.user_id,
        "org_id": cycle.org_id,
        "name": cycle.name,
        "prompt": cycle.prompt,
        "schedule_expr": cycle.schedule_expr,
        "timezone": cycle.timezone,
        "enabled": cycle.enabled,
        "model_override": cycle.model_override,
        "thinking_override": cycle.thinking_override,
        "execution_mode": cycle.execution_mode,
        "target_idea_id": cycle.target_idea_id,
        "reopen_archived": cycle.reopen_archived,
        "next_run_at": _iso(cycle.next_run_at),
        "last_run_at": _iso(cycle.last_run_at),
        "last_status": cycle.last_status,
        "last_error": cycle.last_error,
        "deleted_at": _iso(cycle.deleted_at),
        "created_at": _iso(getattr(cycle, "created_at", None)),
        "updated_at": _iso(getattr(cycle, "updated_at", None)),
    }


def _cycle_run_payload(cycle_run: CycleRun) -> dict[str, Any]:
    return {
        "id": cycle_run.id,
        "cycle_id": cycle_run.cycle_id,
        "scheduled_for": _iso(cycle_run.scheduled_for),
        "started_at": _iso(cycle_run.started_at),
        "completed_at": _iso(cycle_run.completed_at),
        "status": cycle_run.status,
        "error": cycle_run.error,
        "skip_reason": cycle_run.skip_reason,
        "idea_id": cycle_run.idea_id,
        "run_id": cycle_run.run_id,
        "prompt_snapshot": cycle_run.prompt_snapshot,
        "created_at": _iso(getattr(cycle_run, "created_at", None)),
    }


def _trace_diagnostics(
    runs: list[AgentRunRow],
    events: list[dict[str, Any]],
    artifacts: list[dict[str, Any]],
    cycle_state: dict[str, Any],
) -> dict[str, Any]:
    workspace = _workspace_diagnostics(runs)
    delivery = _delivery_signals(events, artifacts)
    status = _run_status_diagnostics(runs, events)
    return _cap_jsonable({
        "schema_version": 1,
        "workspace": workspace,
        "delivery_signals": delivery,
        "run_status": status,
        "cycle_summary": {
            "cycle_count": cycle_state.get("cycle_count"),
            "cycle_run_count": cycle_state.get("cycle_run_count"),
            "cycle_ids": cycle_state.get("cycle_ids") or [],
            "cycle_run_ids": cycle_state.get("cycle_run_ids") or [],
            "error": cycle_state.get("error"),
        },
    })


def _workspace_diagnostics(runs: list[AgentRunRow]) -> list[dict[str, Any]]:
    diagnostics = []
    for run in runs:
        workspace_ref = getattr(run, "workspace_ref", None)
        if not isinstance(workspace_ref, dict):
            workspace_ref = {}
        roots = _workspace_root_candidates(workspace_ref)
        resources = []
        for resource in _safe_list(workspace_ref.get("resources")):
            if not isinstance(resource, dict):
                continue
            path = resource.get("path")
            resources.append({
                "id": resource.get("id"),
                "kind": resource.get("kind") or resource.get("type"),
                "label": resource.get("label") or resource.get("name"),
                "path": path,
                "looks_like_file": _looks_like_file_path(path),
            })
        suspicious_roots = [
            root for root in roots
            if _looks_like_file_path(root.get("path"))
        ]
        diagnostics.append({
            "run_id": getattr(run, "id", None),
            "workspace_roots": roots,
            "suspicious_file_roots": suspicious_roots,
            "resources": resources,
            "resource_count": len(resources),
        })
    return diagnostics


def _workspace_root_candidates(workspace_ref: dict[str, Any]) -> list[dict[str, Any]]:
    candidates = []
    for key in ("resolved_workspace_root", "workspace_root", "worktree_path", "path", "local_path"):
        value = workspace_ref.get(key)
        if isinstance(value, str) and value.strip():
            candidates.append({"source": key, "path": value.strip(), "looks_like_file": _looks_like_file_path(value)})
    materialization = workspace_ref.get("project_context_materialization")
    if isinstance(materialization, dict):
        for item in _safe_list(materialization.get("workspaces")):
            if isinstance(item, dict) and isinstance(item.get("path"), str) and item["path"].strip():
                path = item["path"].strip()
                candidates.append({
                    "source": "project_context_materialization.workspaces",
                    "name": item.get("name"),
                    "path": path,
                    "looks_like_file": _looks_like_file_path(path),
                })
    snapshot = workspace_ref.get("project_context_snapshot")
    if isinstance(snapshot, dict):
        scope = snapshot.get("permission_scope")
        if isinstance(scope, dict):
            for path in _safe_list(scope.get("allowed_paths")):
                if isinstance(path, str) and path.strip():
                    candidates.append({
                        "source": "project_context_snapshot.permission_scope.allowed_paths",
                        "path": path.strip(),
                        "looks_like_file": _looks_like_file_path(path),
                    })
    return candidates


def _delivery_signals(events: list[dict[str, Any]], artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    signals = []
    keywords = (
        "mattermost", "webhook", "http", "post", "payload too large", "too large",
        "16k", "not a directory", "path escapes workspace", "exec_command", "run_script",
        "browser failed", "delivery",
    )
    for event in events:
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        text = json.dumps(payload, default=str).lower()
        if any(keyword in text for keyword in keywords):
            signals.append({
                "source": "event",
                "at": event.get("created_at"),
                "run_id": event.get("run_id"),
                "event_type": event.get("event_type"),
                "tool_name": payload.get("tool_name"),
                "status": payload.get("status"),
                "payload": payload,
            })
    for artifact in artifacts:
        text = " ".join([
            str(artifact.get("title") or ""),
            str(artifact.get("artifact_type") or ""),
            str(artifact.get("text") or ""),
            json.dumps(artifact.get("payload") or {}, default=str),
        ]).lower()
        if any(keyword in text for keyword in keywords):
            signals.append({
                "source": "artifact",
                "at": artifact.get("created_at"),
                "run_id": artifact.get("run_id"),
                "artifact_id": artifact.get("id"),
                "title": artifact.get("title"),
                "artifact_type": artifact.get("artifact_type"),
                "text": artifact.get("text"),
                "payload": artifact.get("payload") or {},
            })
    return signals


def _run_status_diagnostics(runs: list[AgentRunRow], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    changes_by_run: dict[int, list[dict[str, Any]]] = {}
    for event in events:
        if event.get("event_type") != "run.status_changed" or not _is_int_like(event.get("run_id")):
            continue
        run_id = int(event["run_id"])
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        changes_by_run.setdefault(run_id, []).append({
            "at": event.get("created_at"),
            "from_status": payload.get("from_status"),
            "to_status": payload.get("to_status"),
        })
    rows = []
    for run in runs:
        run_id = int(run.id) if _is_int_like(getattr(run, "id", None)) else None
        rows.append({
            "run_id": run_id,
            "status": getattr(run, "status", None),
            "created_at": _iso(getattr(run, "created_at", None)),
            "started_at": _iso(getattr(run, "started_at", None)),
            "completed_at": _iso(getattr(run, "completed_at", None)),
            "failed_at": _iso(getattr(run, "failed_at", None)),
            "canceled_at": _iso(getattr(run, "canceled_at", None)),
            "status_changes": changes_by_run.get(run_id or -1, []),
        })
    return rows


def _looks_like_file_path(path: Any) -> bool:
    return isinstance(path, str) and bool(PurePath(path).suffix)


def _add_int(values: set[int], value: Any) -> None:
    if _is_int_like(value):
        values.add(int(value))


def _tool_trace(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tools = []
    for event in events:
        event_type = event.get("event_type")
        if event_type not in {"run.tool_started", "run.tool_completed", "run.tool_failed"}:
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        tools.append(_cap_jsonable({
            "run_id": event.get("run_id"),
            "event_id": event.get("id"),
            "sequence_no": event.get("sequence_no"),
            "event_type": event_type,
            "tool_name": payload.get("tool_name") or "tool",
            "status": "failed" if event_type == "run.tool_failed" else ("completed" if event_type == "run.tool_completed" else "started"),
            "created_at": event.get("created_at"),
            "args": payload.get("args"),
            "result": payload.get("result"),
            "error": payload.get("error"),
        }))
    return tools


def _truncate_text(value: str, max_chars: int = TRACE_MAX_STRING_CHARS) -> str:
    if len(value) <= max_chars:
        return value
    omitted = len(value) - max_chars
    return f"{value[:max_chars]}... [truncated {omitted} chars]"


def _cap_jsonable(value: Any, *, depth: int = 0) -> Any:
    if depth > TRACE_MAX_DEPTH:
        return "[truncated: max depth]"
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, str):
        return _truncate_text(value)
    if isinstance(value, dict):
        items = list(value.items())
        capped = {
            str(key): _cap_jsonable(item, depth=depth + 1)
            for key, item in items[:TRACE_MAX_COLLECTION_ITEMS]
        }
        if len(items) > TRACE_MAX_COLLECTION_ITEMS:
            capped["_truncated_items"] = len(items) - TRACE_MAX_COLLECTION_ITEMS
        return capped
    if isinstance(value, (list, tuple)):
        capped = [_cap_jsonable(item, depth=depth + 1) for item in list(value)[:TRACE_MAX_COLLECTION_ITEMS]]
        if len(value) > TRACE_MAX_COLLECTION_ITEMS:
            capped.append({"_truncated_items": len(value) - TRACE_MAX_COLLECTION_ITEMS})
        return capped
    return _truncate_text(str(value))


def _contains_truncation(value: Any) -> bool:
    if isinstance(value, str):
        return "[truncated" in value
    if isinstance(value, dict):
        return "_truncated_items" in value or any(_contains_truncation(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_truncation(item) for item in value)
    return False


def _trace_activity_export(snapshot: dict[str, Any]) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    run = snapshot.get("run") if isinstance(snapshot.get("run"), dict) else {}
    runs = _safe_list(snapshot.get("runs")) or ([run] if run else [])
    for run in runs:
        if not isinstance(run, dict):
            continue
        items.append(_cap_jsonable({
            "kind": "run",
            "at": run.get("started_at") or run.get("created_at"),
            "run_id": run.get("run_id"),
            "trace_id": snapshot.get("trace_id"),
            "title": f"run {run.get('status') or 'unknown'}",
            "status": run.get("status"),
            "profile": run.get("profile"),
            "recipe": run.get("recipe"),
            "input_message": run.get("input_message"),
        }))

    thread = snapshot.get("thread") if isinstance(snapshot.get("thread"), dict) else {}
    for message in _safe_list(thread.get("messages")):
        if not isinstance(message, dict):
            continue
        role = message.get("role") or "message"
        items.append(_cap_jsonable({
            "kind": "message",
            "at": message.get("created_at"),
            "message_id": message.get("id"),
            "role": role,
            "title": f"message {role}",
            "message_type": message.get("message_type"),
            "content": message.get("content"),
            "metadata": message.get("metadata") or {},
        }))

    for event in _safe_list(snapshot.get("events")):
        if not isinstance(event, dict):
            continue
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        items.append(_cap_jsonable({
            "kind": "event",
            "at": event.get("created_at"),
            "run_id": event.get("run_id"),
            "event_id": event.get("id"),
            "sequence_no": event.get("sequence_no"),
            "title": event.get("event_type"),
            "event_type": event.get("event_type"),
            "producer": event.get("producer"),
            "visibility": event.get("visibility"),
            "tool_name": payload.get("tool_name"),
            "status": payload.get("status"),
            "payload": payload,
        }))

    for artifact in _safe_list(snapshot.get("artifacts")):
        if not isinstance(artifact, dict):
            continue
        items.append(_cap_jsonable({
            "kind": "artifact",
            "at": artifact.get("created_at"),
            "run_id": artifact.get("run_id"),
            "artifact_id": artifact.get("id"),
            "title": artifact.get("title") or artifact.get("artifact_type"),
            "artifact_type": artifact.get("artifact_type"),
            "visibility": artifact.get("visibility"),
            "uri": artifact.get("uri"),
        }))

    cycles = snapshot.get("cycles") if isinstance(snapshot.get("cycles"), dict) else {}
    for cycle in _safe_list(cycles.get("cycles")):
        if not isinstance(cycle, dict):
            continue
        items.append(_cap_jsonable({
            "kind": "cycle",
            "at": cycle.get("updated_at") or cycle.get("created_at"),
            "cycle_id": cycle.get("id"),
            "title": cycle.get("name") or "cycle",
            "status": cycle.get("last_status"),
            "enabled": cycle.get("enabled"),
            "schedule_expr": cycle.get("schedule_expr"),
            "timezone": cycle.get("timezone"),
            "next_run_at": cycle.get("next_run_at"),
            "last_run_at": cycle.get("last_run_at"),
            "last_error": cycle.get("last_error"),
        }))
    for cycle_run in _safe_list(cycles.get("cycle_runs")):
        if not isinstance(cycle_run, dict):
            continue
        items.append(_cap_jsonable({
            "kind": "cycle_run",
            "at": cycle_run.get("created_at") or cycle_run.get("scheduled_for"),
            "cycle_run_id": cycle_run.get("id"),
            "cycle_id": cycle_run.get("cycle_id"),
            "run_id": cycle_run.get("run_id"),
            "title": f"cycle_run {cycle_run.get('status') or 'unknown'}",
            "status": cycle_run.get("status"),
            "scheduled_for": cycle_run.get("scheduled_for"),
            "started_at": cycle_run.get("started_at"),
            "completed_at": cycle_run.get("completed_at"),
            "error": cycle_run.get("error"),
            "skip_reason": cycle_run.get("skip_reason"),
        }))

    diagnostics = snapshot.get("diagnostics") if isinstance(snapshot.get("diagnostics"), dict) else {}
    for signal in _safe_list(diagnostics.get("delivery_signals")):
        if not isinstance(signal, dict):
            continue
        items.append(_cap_jsonable({
            "kind": "diagnostic",
            "at": signal.get("at"),
            "run_id": signal.get("run_id"),
            "title": "delivery signal",
            "source": signal.get("source"),
            "tool_name": signal.get("tool_name"),
            "status": signal.get("status"),
            "signal": signal,
        }))

    items.sort(key=lambda item: (
        "" if item.get("at") is None else str(item.get("at")),
        int(item.get("sequence_no") or 0),
        str(item.get("kind") or ""),
    ))
    return {
        "schema_version": AGENT_TRACE_EXPORT_SCHEMA_VERSION,
        "trace_id": snapshot.get("trace_id"),
        "run_id": run.get("run_id") if isinstance(run, dict) else None,
        "run_ids": [item.get("run_id") for item in runs if isinstance(item, dict) and item.get("run_id")],
        "idea_id": thread.get("idea_id"),
        "items": _jsonable(items),
        "item_count": len(items),
    }


def _trace_export_readme(snapshot: dict[str, Any]) -> str:
    run = snapshot.get("run") if isinstance(snapshot.get("run"), dict) else {}
    thread = snapshot.get("thread") if isinstance(snapshot.get("thread"), dict) else {}
    estimate = snapshot.get("storage_estimate") if isinstance(snapshot.get("storage_estimate"), dict) else {}
    is_thread_export = snapshot.get("export_scope") == "thread"
    title = "Illo thread trace export" if is_thread_export else "Illo agent trace export"
    scope_note = (
        "This zip is meant to be attached to a debugging conversation so another agent can inspect the full thread conversation and its related runs."
        if is_thread_export
        else "This zip is meant to be attached to a debugging conversation so another agent can inspect why Illo answered the way it did."
    )
    return "\n".join([
        f"# {title}",
        "",
        scope_note,
        "",
        "Files:",
        "- trace.json: the full bounded trace snapshot, including thread messages, run metadata, events, tool calls, artifacts, Cycle rows, CycleRun rows, and diagnostics.",
        "- activity.json: a compact chronological list derived from trace.json, including Cycle/CycleRun and delivery diagnostic items.",
        "- manifest.json: export metadata.",
        "",
        "Notes:",
        "- Large strings and deep values may be truncated in place.",
        "- The export can include prompts, assistant output, tool arguments/results, and artifact metadata.",
        "- Diagnostics highlight workspace roots, file-only resources, run status transitions, Cycle lifecycle state, and delivery/webhook failure signals.",
        "- This export is generated on demand and is not saved as a database artifact.",
        "",
        f"Trace ID: {snapshot.get('trace_id') or 'unknown'}",
        f"Idea ID: {thread.get('idea_id') or 'unknown'}",
        f"Run ID: {run.get('run_id') or 'unknown'}",
        f"Estimated JSON bytes: {estimate.get('json_bytes') or 'unknown'}",
        f"Truncated: {bool(estimate.get('truncated'))}",
        "",
    ])


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


def _redact_trace_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if TRACE_SECRET_KEY_RE.search(key_text):
                redacted[key_text] = TRACE_REDACTED_VALUE
            else:
                redacted[key_text] = _redact_trace_secrets(item)
        return redacted
    if isinstance(value, list):
        return [_redact_trace_secrets(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_trace_secrets(item) for item in value]
    if isinstance(value, str):
        text = value
        for pattern in TRACE_SECRET_VALUE_PATTERNS:
            text = pattern.sub(TRACE_REDACTED_SECRET, text)
        return text
    return value


def _safe_filename_part(value: Any) -> str:
    return "".join(ch if str(ch).isalnum() or ch in {"-", "_"} else "-" for ch in str(value)).strip("-")


def _is_int_like(value: Any) -> bool:
    try:
        int(value)
        return True
    except (TypeError, ValueError):
        return False


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
    "AGENT_TRACE_SNAPSHOT_ARTIFACT_TYPE",
    "agent_trace_export_filename",
    "build_agent_trace_snapshot_async",
    "build_agent_trace_export_zip",
    "build_run_flight_recorder_async",
    "build_run_run_summary_async",
    "build_thread_trace_snapshot_async",
    "load_run_recordings_async",
    "persist_run_recordings",
    "trace_id_for_run_id",
    "trace_id_for_scheduler_run_id",
    "_worker_recordings",
]
