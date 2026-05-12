"""Cortex analytics, timeline, suggested, and slash-commands endpoints."""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from fastapi import Depends, HTTPException
from sqlalchemy import func, select, text

from brain.app.api.auth import get_current_user
from brain.app.api.routers.cortex._router import router
from brain.platform.db.models.agent_run import AgentRunEventRow, AgentRunRow
from brain.platform.db.models.idea import Idea, IdeaStateLog, IdeaThread
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.platform.providers.model_policy import DEFAULT_MODEL_TIER, normalize_model_tier

logger = logging.getLogger(__name__)


# ── Analytics ──────────────────────────────────────────────────

@router.get("/analytics")
async def analytics(user: dict[str, Any] = Depends(get_current_user)):
    async with UnitOfWork() as uow:
        # Complex aggregate — use text() via UnitOfWork session
        result = await uow.session.execute(text("""
            SELECT
                COUNT(*) FILTER (WHERE archived_at IS NULL) AS total_ideas,
                COUNT(*) FILTER (WHERE archived_at IS NULL AND status = 'working') AS working,
                COUNT(*) FILTER (WHERE archived_at IS NULL AND status = 'needs_input') AS awaiting_response,
                COUNT(*) FILTER (WHERE archived_at IS NULL AND status IN ('queued', 'active', 'exploring', 'building', 'testing')) AS queued,
                COALESCE(SUM(active_agents) FILTER (WHERE archived_at IS NULL), 0) AS active_agents,
                COUNT(*) FILTER (WHERE archived_at IS NULL AND created_at::date = CURRENT_DATE) AS ideas_today,
                COUNT(*) FILTER (WHERE archived_at IS NULL AND status = 'resolved' AND updated_at::date = CURRENT_DATE) AS ideas_resolved_today
            FROM ideas
        """))
        row = result.fetchone()

        avg_result = await uow.session.execute(text("""
            SELECT AVG(EXTRACT(EPOCH FROM (t.created_at - i.created_at)) * 1000)::int AS avg_response_time_ms
            FROM idea_threads t
            JOIN ideas i ON t.idea_id = i.id
            WHERE t.role = 'illo'
              AND t.created_at::date = CURRENT_DATE
              AND t.created_at = (
                  SELECT MIN(t2.created_at) FROM idea_threads t2
                  WHERE t2.idea_id = i.id AND t2.role = 'illo'
              )
        """))
        avg_row = avg_result.fetchone()

    result_dict = dict(row._mapping)
    result_dict["avg_response_time_ms"] = (avg_row._mapping["avg_response_time_ms"] or 0) if avg_row else 0
    return result_dict


@router.get("/ideas/{idea_id}/activity-timeline")
async def activity_timeline(idea_id: str, user: dict[str, Any] = Depends(get_current_user)):
    async with UnitOfWork() as uow:
        idea = await uow.session.get(Idea, idea_id)
        if not idea:
            raise HTTPException(status_code=404, detail=f"Idea {idea_id} not found")

        events = []
        events.append({
            "type": "created",
            "label": "Created",
            "timestamp": idea.created_at.isoformat() if isinstance(idea.created_at, datetime) else idea.created_at,
        })

        stmt = (
            select(IdeaStateLog)
            .where(IdeaStateLog.idea_id == idea_id)
            .order_by(IdeaStateLog.changed_at)
        )
        state_rows = await uow.session.scalars(stmt)
        for row in state_rows.all():
            ts = row.changed_at
            events.append({
                "type": "state_change",
                "label": f"{row.from_state or '-'} -> {row.to_state}"
                         + (f" ({row.trigger})" if row.trigger else ""),
                "timestamp": ts.isoformat() if isinstance(ts, datetime) else ts,
            })

        stmt = (
            select(IdeaThread)
            .where(IdeaThread.idea_id == idea_id)
            .order_by(IdeaThread.created_at)
        )
        role_map = {"user": "You replied", "illo": "Illo replied", "assistant": "Assistant replied"}
        thread_rows = await uow.session.scalars(stmt)
        for row in thread_rows.all():
            prefix = role_map.get(row.role, f"{row.role} replied")
            snippet = (row.content or "")[:80]
            if len(row.content or "") > 80:
                snippet += "..."
            ts = row.created_at
            events.append({
                "type": "thread_message",
                "label": f'{prefix}: "{snippet}"',
                "timestamp": ts.isoformat() if isinstance(ts, datetime) else ts,
            })

        details = list(idea.agent_details or [])
        for agent in details:
            if agent.get("started_at"):
                events.append({
                    "type": "agent_started",
                    "label": f"Agent started: \"{agent.get('label', '?')}\" ({agent.get('skill', '?')})",
                    "timestamp": agent["started_at"],
                })
            if agent.get("finished_at"):
                result_snippet = (agent.get("result") or agent.get("error") or "")[:60]
                events.append({
                    "type": "agent_finished",
                    "label": f"Agent {agent.get('status', 'done')}: \"{agent.get('label', '?')}\""
                             + (f" - {result_snippet}" if result_snippet else ""),
                    "timestamp": agent["finished_at"],
                })

        stmt = (
            select(
                AgentRunEventRow.payload,
                AgentRunEventRow.created_at,
                AgentRunRow.id.label("run_id"),
            )
            .join(AgentRunRow, AgentRunEventRow.run_id == AgentRunRow.id)
            .where(
                AgentRunRow.thread_id == idea_id,
                AgentRunEventRow.event_type.in_(["run.tool_started", "run.tool_completed", "run.tool_failed"]),
            )
            .order_by(AgentRunEventRow.created_at)
        )
        tool_rows = await uow.session.execute(stmt)
        for row in tool_rows.all():
            ts = row.created_at
            payload = row.payload or {}
            try:
                args_obj = payload.get("args") if isinstance(payload.get("args"), dict) else {}
                arg_parts = [f"{v}"[:50] for k, v in list(args_obj.items())[:2]]
                arg_label = " - ".join(arg_parts)
            except Exception:
                arg_label = ""
            tool_name = str(payload.get("tool_name") or "tool")
            label = f"{tool_name}" + (f": {arg_label}" if arg_label else "")
            events.append({
                "type": "tool_call",
                "label": label,
                "tool_name": tool_name,
                "run_id": row.run_id,
                "timestamp": ts.isoformat() if isinstance(ts, datetime) else ts,
            })

    events.sort(key=lambda e: e["timestamp"])
    return events

# ── Suggested / Slash commands ─────────────────────────────────

@router.get("/suggested")
async def suggested_idea(user: dict[str, Any] = Depends(get_current_user)):
    async with UnitOfWork() as uow:
        # Complex query with CASE ordering and subquery — use text() via UnitOfWork
        result = await uow.session.execute(text("""
            SELECT id, title, display_title, status, updated_at,
                   EXTRACT(EPOCH FROM (NOW() - updated_at)) / 3600.0 as hours_waiting,
                   (SELECT COUNT(*) FROM idea_threads WHERE idea_id = ideas.id) as thread_count
            FROM ideas
            WHERE archived_at IS NULL AND status IN ('needs_input', 'unread_reply', 'active')
            ORDER BY
                CASE status
                    WHEN 'needs_input' THEN 0
                    WHEN 'unread_reply' THEN 1
                    WHEN 'active' THEN 2
                END,
                updated_at ASC
            LIMIT 1
        """))
        row = result.fetchone()
    if not row:
        return {"suggestion": None}
    m = row._mapping
    return {"suggestion": {
        'id': str(m['id']),
        'title': m['display_title'] or m['title'],
        'status': m['status'],
        'hours_waiting': round(m['hours_waiting'], 1),
        'thread_count': m['thread_count']
    }}


@router.get("/slash-commands")
async def api_slash_commands(user: dict[str, Any] = Depends(get_current_user)):
    from brain.systems.skills.builtin import ensure_builtin_skills_cached

    await ensure_builtin_skills_cached()
    async with UnitOfWork() as uow:
        skills = await uow.skills.list_command_summaries()
    result = []
    for s in skills:
        use_count = int(getattr(s, "use_count", 0) or 0)
        success_count = int(getattr(s, "success_count", 0) or 0)
        result.append({
            "name": s.name,
            "description": s.description,
            "model_tier": normalize_model_tier(s.model_tier) or DEFAULT_MODEL_TIER,
            "maturity": s.maturity,
            "use_count": use_count,
            "success_count": success_count,
            "success_rate": round(success_count / use_count, 3) if use_count > 0 else 0,
            "type": "skill",
        })
    return result
