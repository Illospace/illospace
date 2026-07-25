"""AgentRun audit summaries for Cortex."""

from __future__ import annotations

from collections import Counter
from typing import Any

from sqlalchemy import select

from brain.platform.db.models.agent_run import AgentRunEventRow, AgentRunRow
from brain.systems.runs.token_usage import async_summarize_runs_usage


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


async def async_build_idea_audit_summary(session, idea_id: str) -> dict[str, Any]:
    runs = (
        await session.scalars(
            select(AgentRunRow)
            .where(AgentRunRow.thread_id == idea_id)
            .order_by(AgentRunRow.created_at.asc(), AgentRunRow.id.asc())
        )
    ).all()
    if not runs:
        raise RunAuditNotFound(f"No runs for idea {idea_id}")
    run_ids = [int(run.id) for run in runs]
    events = (
        await session.scalars(
            select(AgentRunEventRow)
            .where(AgentRunEventRow.run_id.in_(run_ids))
            .order_by(AgentRunEventRow.id.asc())
        )
    ).all()
    usage = await async_summarize_runs_usage(session, runs)
    return _idea_audit_summary_payload(idea_id, list(runs), list(events), usage=usage)


def _idea_audit_summary_payload(
    idea_id: str,
    runs: list[AgentRunRow],
    events: list[AgentRunEventRow],
    *,
    usage: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    usage_by_run = {
        int(item["id"]): item
        for item in usage or []
        if item.get("id") is not None
    }
    token_totals = {
        "api_calls": sum(int(item.get("api_calls") or 0) for item in usage_by_run.values()),
        "tokens_input": sum(int(item.get("tokens_input") or 0) for item in usage_by_run.values()),
        "tokens_output": sum(int(item.get("tokens_output") or 0) for item in usage_by_run.values()),
        "tokens_total": sum(int(item.get("tokens_total") or 0) for item in usage_by_run.values()),
        "estimated_cost": round(
            sum(float(item.get("estimated_cost") or 0) for item in usage_by_run.values()),
            6,
        ),
    }
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
                "usage": {
                    key: usage_by_run[int(run.id)].get(key)
                    for key in (
                        "api_calls",
                        "tokens_input",
                        "tokens_output",
                        "tokens_total",
                        "estimated_cost",
                        "by_effort",
                    )
                }
                if int(run.id) in usage_by_run
                else None,
            }
            for run in runs
        ],
        "usage": token_totals,
        "event_count": len(events),
        "tool_summary": build_tool_summary(events),
    }


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


__all__ = [
    "RunAuditNotFound",
    "async_build_idea_audit_summary",
    "build_tool_summary",
]
