"""Read models for durable agent handoff summaries attached to Cortex threads."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.agent import AgentSession
from brain.platform.db.models.agent_run import AgentRunRow

_RUN_SESSION_RE = re.compile(r"^agent-run-(?P<run_id>\d+)(?:-|$)")


async def latest_thread_handoff_summary(
    session: AsyncSession,
    idea_id: str,
) -> dict[str, Any]:
    """Return the newest durable handoff summary for runs in one thread."""
    run_result = await session.scalars(
        select(AgentRunRow.id)
        .where(AgentRunRow.thread_id == str(idea_id))
        .order_by(AgentRunRow.created_at.desc(), AgentRunRow.id.desc())
    )
    run_ids = [int(run_id) for run_id in run_result.all()]
    if not run_ids:
        return {"found": False}

    candidates_by_session_id = {
        session_id: run_id
        for run_id in run_ids
        for session_id in _candidate_session_ids(run_id)
    }
    session_result = await session.scalars(
        select(AgentSession).where(AgentSession.session_id.in_(list(candidates_by_session_id)))
    )
    summaries = [
        agent_session
        for agent_session in session_result.all()
        if _handoff_payload(agent_session.handoff_summary)
    ]
    if not summaries:
        return {"found": False}

    latest = max(summaries, key=_handoff_sort_key)
    payload = _handoff_payload(latest.handoff_summary) or {}
    run_id = candidates_by_session_id.get(latest.session_id) or _run_id_from_session_id(latest.session_id)
    updated_at = _coerce_datetime(latest.handoff_updated_at or latest.updated_at or latest.created_at)

    return {
        "found": True,
        "run_id": run_id,
        "session_id": latest.session_id,
        "updated_at": updated_at.isoformat() if updated_at else None,
        "message_count": int(
            latest.handoff_message_count
            or payload.get("message_count")
            or 0
        ),
        "summary": payload,
    }


def _candidate_session_ids(run_id: int) -> tuple[str, str]:
    return (f"agent-run-{run_id}", f"agent-run-{run_id}-worker")


def _handoff_payload(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value if value else None
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) and parsed else None
    return None


def _handoff_sort_key(session: AgentSession) -> tuple[datetime, str]:
    when = _coerce_datetime(session.handoff_updated_at or session.updated_at or session.created_at)
    return (when or datetime.min.replace(tzinfo=timezone.utc), session.session_id)


def _coerce_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _run_id_from_session_id(session_id: str) -> int | None:
    match = _RUN_SESSION_RE.match(str(session_id or ""))
    if not match:
        return None
    return int(match.group("run_id"))


__all__ = ["latest_thread_handoff_summary"]
