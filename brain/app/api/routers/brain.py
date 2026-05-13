"""Brain router — health score, learnings, stale ideas, admin, search."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.app.api.auth import get_current_user
from brain.app.api.deps import get_db, rate_limit
from brain.platform.db.models.run import AgentRun
from brain.platform.db.models.idea import Idea, IdeaThread
from brain.platform.db.models.memory import Memory
from brain.platform.db.models.org import User
from brain.platform.db.models.skill import Skill, SkillExecution
from brain.platform.db.models.system import ConsolidationRun, RetrievalLog

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api",
    tags=["brain"],
    dependencies=[Depends(rate_limit)],
)


# ── Helpers ──────────────────────────────────────────────────────────

def _safe_float(v: Any, default: float = 0.0) -> float:
    if v is None:
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _health_color(score: int) -> str:
    if score >= 80:
        return "green"
    if score >= 50:
        return "yellow"
    return "red"


def _health_label(score: int) -> str:
    if score >= 80:
        return "healthy"
    if score >= 50:
        return "fair"
    return "degraded"


# ═══════════════════════════════════════════
# Brain Health
# ═══════════════════════════════════════════

@router.get("/brain-health")
async def brain_health(
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Composite 0-100 brain health score from sub-signals."""
    now = datetime.now(timezone.utc)

    cutoff_7d = now - timedelta(days=7)
    total_stmt = select(func.count(RetrievalLog.id)).where(
        RetrievalLog.timestamp >= cutoff_7d
    )
    hit_stmt = select(func.count(RetrievalLog.id)).where(
        RetrievalLog.timestamp >= cutoff_7d,
        RetrievalLog.feedback == "hit",
    )
    total = await db.scalar(total_stmt) or 0
    hits = await db.scalar(hit_stmt) or 0
    retrieval_accuracy = round(hits / total, 3) if total > 0 else None

    last_cons_stmt = (
        select(ConsolidationRun.completed_at)
        .where(ConsolidationRun.status == "completed")
        .order_by(ConsolidationRun.completed_at.desc())
        .limit(1)
    )
    last_cons = await db.scalar(last_cons_stmt)
    if last_cons:
        days_since = (now - last_cons.replace(tzinfo=timezone.utc)).days
    else:
        days_since = 999

    has_embedding_stmt = select(func.count(Memory.id)).where(
        Memory.semantic_embedding.isnot(None)
    ).limit(1)
    embedding_ok = (await db.scalar(has_embedding_stmt) or 0) > 0

    cutoff_48h = now - timedelta(hours=48)
    skill_total_stmt = select(func.count(SkillExecution.id)).where(
        SkillExecution.started_at >= cutoff_48h
    )
    skill_fail_stmt = select(func.count(SkillExecution.id)).where(
        SkillExecution.started_at >= cutoff_48h,
        SkillExecution.outcome == "failure",
    )
    skill_total = await db.scalar(skill_total_stmt) or 0
    skill_fails = await db.scalar(skill_fail_stmt) or 0
    skill_error_rate = round(skill_fails / skill_total, 3) if skill_total > 0 else 0.0

    retrieval_score = (retrieval_accuracy * 100) if retrieval_accuracy is not None else 70
    consolidation_score = max(0, 100 - days_since * 10)
    embedding_score = 100 if embedding_ok else 30
    error_score = max(0, 100 - skill_error_rate * 200)

    score = int(
        retrieval_score * 0.30
        + consolidation_score * 0.25
        + embedding_score * 0.20
        + error_score * 0.25
    )
    score = max(0, min(100, score))

    return {
        "score": score,
        "label": _health_label(score),
        "color": _health_color(score),
        "components": {
            "retrieval_accuracy": retrieval_accuracy,
            "days_since_consolidation": days_since if days_since < 999 else None,
            "embedding_server_ok": embedding_ok,
            "skill_error_rate_48h": skill_error_rate,
        },
    }


# ═══════════════════════════════════════════
# Recent Learnings
# ═══════════════════════════════════════════

@router.get("/recent-learnings")
async def recent_learnings(
    hours: int = Query(48, ge=1, le=720),
    min_salience: float = Query(7, ge=0, le=10),
    limit: int = Query(5, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """High-salience memories from the last N hours."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    stmt = (
        select(Memory)
        .where(
            Memory.created_at >= cutoff,
            Memory.salience >= min_salience,
            or_(Memory.archived == False, Memory.archived.is_(None)),  # noqa: E712
        )
        .order_by(Memory.salience.desc())
        .limit(limit)
    )
    result = await db.scalars(stmt)
    return [
        {
            "type": memory.memory_type,
            "salience": _safe_float(memory.salience),
            "content": (memory.content or "")[:300],
            "source": memory.source if hasattr(memory, "source") else None,
            "created_at": memory.created_at.isoformat() if memory.created_at else None,
        }
        for memory in result.all()
    ]


# ═══════════════════════════════════════════
# Stale Ideas
# ═══════════════════════════════════════════

@router.get("/stale-ideas")
async def stale_ideas(
    threshold: int = Query(30, ge=1),
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Ideas stuck in 'working' status with no thread activity > threshold minutes."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=threshold)
    latest_thread = (
        select(
            IdeaThread.idea_id,
            func.max(IdeaThread.created_at).label("last_thread"),
        )
        .group_by(IdeaThread.idea_id)
        .subquery()
    )

    stmt = (
        select(
            Idea.id,
            Idea.title,
            Idea.updated_at,
            latest_thread.c.last_thread,
        )
        .outerjoin(latest_thread, Idea.id == latest_thread.c.idea_id)
        .where(
            Idea.status == "working",
            Idea.archived_at.is_(None),
            or_(
                latest_thread.c.last_thread < cutoff,
                latest_thread.c.last_thread.is_(None),
            ),
            Idea.updated_at < cutoff,
        )
        .order_by(Idea.updated_at.asc())
    )
    result = await db.execute(stmt)
    now = datetime.now(timezone.utc)
    return [
        {
            "idea_id": row.id,
            "display_title": (row.title or "Untitled")[:100],
            "minutes_stale": int(
                (now - (row.last_thread or row.updated_at).replace(tzinfo=timezone.utc)).total_seconds() / 60
            ),
        }
        for row in result.all()
    ]


# Admin — Pending Users
# ═══════════════════════════════════════════

def _require_owner(user: dict[str, Any]):
    if user.get("role") not in ("owner", "admin"):
        raise HTTPException(status_code=403, detail="Owner access required")


def _require_workspace_member(user: dict[str, Any]) -> str:
    org_id = user.get("org_id")
    if user.get("principal_type") == "service" or not org_id or user.get("approved") is False:
        raise HTTPException(status_code=403, detail="Workspace member access required")
    return str(org_id)


@router.get("/admin/pending")
async def list_pending_users(
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """List pending (unapproved) users. Owner only."""
    _require_owner(user)
    stmt = (
        select(User)
        .where(User.approved == False)  # noqa: E712
        .order_by(User.created_at.asc())
    )
    result = await db.scalars(stmt)
    return [
        {
            "id": pending_user.id,
            "name": pending_user.name,
            "email": pending_user.email,
            "created_at": pending_user.created_at.isoformat() if pending_user.created_at else None,
        }
        for pending_user in result.all()
    ]


@router.post("/admin/users/{user_id}/approve")
async def approve_user(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Approve a pending user in the current workspace."""
    org_id = _require_workspace_member(user)
    target = await db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if str(target.org_id) != org_id:
        raise HTTPException(status_code=404, detail="User not found")
    if target.approved:
        raise HTTPException(status_code=409, detail="User is already approved")
    target.approved = True
    return {"ok": True, "user_id": user_id, "approved": True}


@router.post("/admin/users/{user_id}/reject")
async def reject_user(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Reject (delete) a pending user. Owner only."""
    _require_owner(user)
    target = await db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if str(target.org_id) != str(user.get("org_id")):
        raise HTTPException(status_code=404, detail="User not found")
    if target.approved:
        raise HTTPException(status_code=409, detail="User is already approved")
    await db.delete(target)
    return {"ok": True, "user_id": user_id, "rejected": True}


# ═══════════════════════════════════════════
# Search
# ═══════════════════════════════════════════

@router.get("/search")
async def global_search(
    q: str = Query(..., min_length=2),
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Full-text search across memories and skills."""
    pattern = f"%{q}%"

    mem_stmt = (
        select(Memory)
        .where(
            Memory.content.ilike(pattern),
            or_(Memory.archived == False, Memory.archived.is_(None)),  # noqa: E712
        )
        .order_by(Memory.salience.desc())
        .limit(10)
    )
    memory_result = await db.scalars(mem_stmt)

    skill_stmt = (
        select(Skill)
        .where(
            or_(
                Skill.name.ilike(pattern),
                Skill.description.ilike(pattern),
            ),
            or_(Skill.archived == False, Skill.archived.is_(None)),  # noqa: E712
        )
        .order_by(Skill.name)
        .limit(10)
    )
    skill_result = await db.scalars(skill_stmt)

    return {
        "memories": [
            {
                "id": memory.id,
                "content": (memory.content or "")[:200],
                "type": memory.memory_type,
                "salience": _safe_float(memory.salience),
            }
            for memory in memory_result.all()
        ],
        "skills": [
            {
                "id": skill.id,
                "name": skill.name,
                "description": (skill.description or "")[:200],
                "maturity": skill.maturity if hasattr(skill, "maturity") else None,
            }
            for skill in skill_result.all()
        ],
    }
