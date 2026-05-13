"""Run trace/recording projection helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
import json
from typing import Any
import zipfile

from sqlalchemy import select

from brain.systems.runs.ids import trace_id_for_run_id
from brain.platform.db.models.agent_run import AgentRunArtifactRow, AgentRunEventRow, AgentRunRow
from brain.platform.db.models.idea import IdeaThread


def build_run_run_summary(session, run_id: int) -> dict[str, Any]:
    run = session.get(AgentRunRow, int(run_id))
    return _run_summary(run, run_id)


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
TRACE_MAX_STRING_CHARS = 4000
TRACE_MAX_COLLECTION_ITEMS = 80
TRACE_MAX_DEPTH = 6


def build_agent_trace_snapshot(
    session,
    run: AgentRunRow,
    *,
    saved_by: str | None = None,
    max_messages: int = TRACE_MAX_MESSAGES,
    max_events: int = TRACE_MAX_EVENTS,
    max_artifacts: int = TRACE_MAX_ARTIFACTS,
) -> dict[str, Any]:
    """Build a bounded, JSON-safe trace bundle suitable for later analysis."""

    related_run_ids = _related_run_ids(session, run)
    messages = _trace_messages(session, run, max_messages=max_messages)
    events = _trace_events(session, related_run_ids, max_events=max_events)
    artifacts = _trace_artifacts(session, related_run_ids, max_artifacts=max_artifacts)
    return _agent_trace_snapshot_payload(
        run,
        related_run_ids=related_run_ids,
        messages=messages,
        events=events,
        artifacts=artifacts,
        saved_by=saved_by,
        max_messages=max_messages,
        max_events=max_events,
        max_artifacts=max_artifacts,
    )


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
    messages = await _trace_messages_async(session, run, max_messages=max_messages)
    events = await _trace_events_async(session, related_run_ids, max_events=max_events)
    artifacts = await _trace_artifacts_async(session, related_run_ids, max_artifacts=max_artifacts)
    return _agent_trace_snapshot_payload(
        run,
        related_run_ids=related_run_ids,
        messages=messages,
        events=events,
        artifacts=artifacts,
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
    }
    bundle["storage_estimate"] = {
        "json_bytes": len(json.dumps(_jsonable(bundle), sort_keys=True, default=str).encode("utf-8")),
        "truncated": _contains_truncation(bundle),
    }
    return _jsonable(bundle)


def build_thread_trace_snapshot(
    session,
    idea_id: str,
    *,
    saved_by: str | None = None,
    max_messages: int | None = THREAD_TRACE_MAX_MESSAGES,
    max_runs: int = THREAD_TRACE_MAX_RUNS,
    max_events: int = THREAD_TRACE_MAX_EVENTS,
    max_artifacts: int = THREAD_TRACE_MAX_ARTIFACTS,
) -> dict[str, Any]:
    """Build a bounded export for analyzing a whole Cortex thread conversation."""

    runs = _thread_runs(session, idea_id, max_runs=max_runs)
    run_ids = [int(run.id) for run in runs if _is_int_like(getattr(run, "id", None))]
    messages = _trace_thread_messages(session, idea_id, max_messages=max_messages)
    events = _trace_events(session, run_ids, max_events=max_events)
    artifacts = _trace_artifacts(session, run_ids, max_artifacts=max_artifacts)
    return _thread_trace_snapshot_payload(
        idea_id,
        runs=runs,
        run_ids=run_ids,
        messages=messages,
        events=events,
        artifacts=artifacts,
        saved_by=saved_by,
        max_messages=max_messages,
        max_runs=max_runs,
        max_events=max_events,
        max_artifacts=max_artifacts,
    )


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
    return _thread_trace_snapshot_payload(
        idea_id,
        runs=runs,
        run_ids=run_ids,
        messages=messages,
        events=events,
        artifacts=artifacts,
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
    }
    bundle["storage_estimate"] = {
        "json_bytes": len(json.dumps(_jsonable(bundle), sort_keys=True, default=str).encode("utf-8")),
        "truncated": _contains_truncation(bundle),
    }
    return _jsonable(bundle)


def build_agent_trace_export_zip(
    snapshot: dict[str, Any],
) -> bytes:
    """Package a trace snapshot as a small shareable zip."""

    trace = _jsonable(snapshot)
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


def _related_run_ids(session, run: AgentRunRow) -> list[int]:
    root_run_id = getattr(run, "root_run_id", None) or getattr(run, "id", None)
    rows = session.scalars(
        select(AgentRunRow.id)
        .where((AgentRunRow.id == int(root_run_id)) | (AgentRunRow.root_run_id == int(root_run_id)))
        .order_by(AgentRunRow.created_at.asc(), AgentRunRow.id.asc())
    ).all()
    ids = _coerce_run_ids(rows)
    if int(run.id) not in ids:
        ids.insert(0, int(run.id))
    return ids


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


def _thread_runs(session, idea_id: str, *, max_runs: int) -> list[AgentRunRow]:
    return session.scalars(
        select(AgentRunRow)
        .where(AgentRunRow.thread_id == str(idea_id))
        .order_by(AgentRunRow.created_at.asc(), AgentRunRow.id.asc())
        .limit(max_runs)
    ).all()


async def _thread_runs_async(session, idea_id: str, *, max_runs: int) -> list[AgentRunRow]:
    result = await session.scalars(
        select(AgentRunRow)
        .where(AgentRunRow.thread_id == str(idea_id))
        .order_by(AgentRunRow.created_at.asc(), AgentRunRow.id.asc())
        .limit(max_runs)
    )
    return list(result.all())


def _trace_messages(session, run: AgentRunRow, *, max_messages: int) -> list[dict[str, Any]]:
    return _trace_thread_messages(session, str(run.thread_id), max_messages=max_messages)


async def _trace_messages_async(session, run: AgentRunRow, *, max_messages: int) -> list[dict[str, Any]]:
    return await _trace_thread_messages_async(session, str(run.thread_id), max_messages=max_messages)


def _trace_thread_messages(session, idea_id: str, *, max_messages: int | None) -> list[dict[str, Any]]:
    query = select(IdeaThread).where(IdeaThread.idea_id == str(idea_id))
    if max_messages is None:
        rows = session.scalars(
            query.order_by(IdeaThread.created_at.asc(), IdeaThread.id.asc())
        ).all()
    else:
        limited_rows = session.scalars(
            query
            .order_by(IdeaThread.created_at.desc(), IdeaThread.id.desc())
            .limit(max_messages)
        ).all()
        rows = list(reversed(limited_rows))
    return _thread_message_payloads(rows)


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


def _trace_events(session, run_ids: list[int], *, max_events: int) -> list[dict[str, Any]]:
    if not run_ids:
        return []
    rows = session.scalars(
        select(AgentRunEventRow)
        .where(AgentRunEventRow.run_id.in_(run_ids))
        .order_by(AgentRunEventRow.run_id.asc(), AgentRunEventRow.sequence_no.asc(), AgentRunEventRow.id.asc())
        .limit(max_events)
    ).all()
    return _event_payloads(rows)


async def _trace_events_async(session, run_ids: list[int], *, max_events: int) -> list[dict[str, Any]]:
    if not run_ids:
        return []
    result = await session.scalars(
        select(AgentRunEventRow)
        .where(AgentRunEventRow.run_id.in_(run_ids))
        .order_by(AgentRunEventRow.run_id.asc(), AgentRunEventRow.sequence_no.asc(), AgentRunEventRow.id.asc())
        .limit(max_events)
    )
    return _event_payloads(result.all())


def _event_payloads(rows: list[AgentRunEventRow]) -> list[dict[str, Any]]:
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


def _trace_artifacts(session, run_ids: list[int], *, max_artifacts: int) -> list[dict[str, Any]]:
    if not run_ids:
        return []
    rows = session.scalars(
        select(AgentRunArtifactRow)
        .where(
            AgentRunArtifactRow.run_id.in_(run_ids),
            AgentRunArtifactRow.artifact_type != AGENT_TRACE_SNAPSHOT_ARTIFACT_TYPE,
        )
        .order_by(AgentRunArtifactRow.created_at.asc(), AgentRunArtifactRow.id.asc())
        .limit(max_artifacts)
    ).all()
    return _artifact_payloads(rows)


async def _trace_artifacts_async(session, run_ids: list[int], *, max_artifacts: int) -> list[dict[str, Any]]:
    if not run_ids:
        return []
    result = await session.scalars(
        select(AgentRunArtifactRow)
        .where(
            AgentRunArtifactRow.run_id.in_(run_ids),
            AgentRunArtifactRow.artifact_type != AGENT_TRACE_SNAPSHOT_ARTIFACT_TYPE,
        )
        .order_by(AgentRunArtifactRow.created_at.asc(), AgentRunArtifactRow.id.asc())
        .limit(max_artifacts)
    )
    return _artifact_payloads(result.all())


def _artifact_payloads(rows: list[AgentRunArtifactRow]) -> list[dict[str, Any]]:
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
        "- trace.json: the full bounded trace snapshot, including thread messages, run metadata, events, tool calls, and artifacts.",
        "- activity.json: a compact chronological list derived from trace.json.",
        "- manifest.json: export metadata.",
        "",
        "Notes:",
        "- Large strings and deep values may be truncated in place.",
        "- The export can include prompts, assistant output, tool arguments/results, and artifact metadata.",
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
    "build_agent_trace_snapshot",
    "build_agent_trace_snapshot_async",
    "build_agent_trace_export_zip",
    "build_run_flight_recorder",
    "build_run_run_summary",
    "build_run_run_summary_async",
    "build_thread_trace_snapshot",
    "build_thread_trace_snapshot_async",
    "load_run_recordings",
    "persist_run_recordings",
    "trace_id_for_run_id",
    "trace_id_for_scheduler_run_id",
    "_worker_recordings",
]
