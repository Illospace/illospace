"""AgentRun audit summaries for Cortex."""

from __future__ import annotations

from collections import Counter
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from brain.platform.db.models.agent_run import AgentRunEventRow, AgentRunRow


class RunAuditNotFound(LookupError):
    pass


def build_tool_summary(events_or_runs: list[Any], runner_tools: list[dict[str, Any]] | None = None) -> dict[str, Any] | list[dict[str, Any]]:
    if runner_tools is not None:
        counts: Counter[str] = Counter()
        for tool in runner_tools or []:
            if not isinstance(tool, dict):
                continue
            name = tool.get("tool_name") or tool.get("name")
            if name:
                counts[str(name)] += int(tool.get("count") or 0)
        for run in events_or_runs or []:
            for worker in getattr(run, "workers_used", None) or []:
                if not isinstance(worker, dict):
                    continue
                for name in worker.get("tool_names") or []:
                    if name:
                        counts[str(name)] += 1
        return [
            {"tool_name": name, "count": count}
            for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        ]

    names = []
    for event in events_or_runs or []:
        payload = getattr(event, "payload", None)
        if isinstance(event, dict):
            payload = event.get("payload", event)
        if isinstance(payload, dict):
            name = payload.get("tool_name") or payload.get("tool")
            if name:
                names.append(str(name))
    counts = Counter(names)
    return {
        "count": len(names),
        "tools": [{"tool_name": name, "count": count} for name, count in sorted(counts.items())],
    }


def build_idea_audit_summary(session: Session, idea_id: str) -> dict[str, Any]:
    runs = session.scalars(
        select(AgentRunRow)
        .where(AgentRunRow.thread_id == idea_id)
        .order_by(AgentRunRow.created_at.asc(), AgentRunRow.id.asc())
    ).all()
    if not runs:
        raise RunAuditNotFound(f"No runs for idea {idea_id}")
    run_ids = [int(run.id) for run in runs]
    events = session.scalars(
        select(AgentRunEventRow)
        .where(AgentRunEventRow.run_id.in_(run_ids))
        .order_by(AgentRunEventRow.id.asc())
    ).all()
    return {
        "idea_id": idea_id,
        "run_count": len(runs),
        "runs": [
            {
                "id": run.id,
                "trace_id": run.trace_id,
                "profile": run.profile,
                "recipe": run.recipe,
                "status": run.status,
                "created_at": _iso(run.created_at),
                "completed_at": _iso(run.completed_at),
            }
            for run in runs
        ],
        "event_count": len(events),
        "tool_summary": build_tool_summary(events),
    }


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


__all__ = ["RunAuditNotFound", "build_idea_audit_summary", "build_tool_summary"]
