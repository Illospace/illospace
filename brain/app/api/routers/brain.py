"""Brain router — health score, learnings, stale ideas, prompts, admin, search."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from brain.app.api.auth import get_current_user
from brain.app.api.deps import get_db, rate_limit
from brain.platform.db.models.run import AgentRun
from brain.platform.db.models.emotion import EmotionalSnapshot
from brain.platform.db.models.idea import Idea, IdeaThread
from brain.platform.db.models.memory import Memory
from brain.platform.db.models.org import User
from brain.platform.db.models.prompt import BrainPrompt
from brain.platform.db.models.skill import Skill, SkillExecution
from brain.platform.db.models.system import ConsolidationRun, RetrievalLog
from brain.platform.db.models.task import Task

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
def brain_health(
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Composite 0-100 brain health score from sub-signals."""
    now = datetime.now(timezone.utc)

    # 1. Retrieval accuracy (7d) — weight 30
    cutoff_7d = now - timedelta(days=7)
    total_stmt = select(func.count(RetrievalLog.id)).where(
        RetrievalLog.timestamp >= cutoff_7d
    )
    hit_stmt = select(func.count(RetrievalLog.id)).where(
        RetrievalLog.timestamp >= cutoff_7d,
        RetrievalLog.feedback == "hit",
    )
    total = db.scalar(total_stmt) or 0
    hits = db.scalar(hit_stmt) or 0
    retrieval_accuracy = round(hits / total, 3) if total > 0 else None

    # 2. Days since last consolidation — weight 25
    last_cons_stmt = (
        select(ConsolidationRun.completed_at)
        .where(ConsolidationRun.status == "completed")
        .order_by(ConsolidationRun.completed_at.desc())
        .limit(1)
    )
    last_cons = db.scalar(last_cons_stmt)
    if last_cons:
        days_since = (now - last_cons.replace(tzinfo=timezone.utc)).days
    else:
        days_since = 999

    # 3. Embedding server OK — weight 20 (check if any memory has embedding)
    has_embedding_stmt = select(func.count(Memory.id)).where(
        Memory.semantic_embedding.isnot(None)
    ).limit(1)
    embedding_ok = (db.scalar(has_embedding_stmt) or 0) > 0

    # 4. Skill error rate 48h — weight 25
    cutoff_48h = now - timedelta(hours=48)
    skill_total_stmt = select(func.count(SkillExecution.id)).where(
        SkillExecution.started_at >= cutoff_48h
    )
    skill_fail_stmt = select(func.count(SkillExecution.id)).where(
        SkillExecution.started_at >= cutoff_48h,
        SkillExecution.outcome == "failure",
    )
    skill_total = db.scalar(skill_total_stmt) or 0
    skill_fails = db.scalar(skill_fail_stmt) or 0
    skill_error_rate = round(skill_fails / skill_total, 3) if skill_total > 0 else 0.0

    # Compute component scores (0-100 each)
    retrieval_score = (retrieval_accuracy * 100) if retrieval_accuracy is not None else 70
    consolidation_score = max(0, 100 - days_since * 10)  # 0 after 10 days
    embedding_score = 100 if embedding_ok else 30
    error_score = max(0, 100 - skill_error_rate * 200)  # 50% error rate = 0

    # Weighted average
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
def recent_learnings(
    hours: int = Query(48, ge=1, le=720),
    min_salience: float = Query(7, ge=0, le=10),
    limit: int = Query(5, ge=1, le=50),
    db: Session = Depends(get_db),
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
    rows = db.scalars(stmt).all()
    return [
        {
            "type": m.memory_type,
            "salience": _safe_float(m.salience),
            "content": (m.content or "")[:300],
            "source": m.source if hasattr(m, "source") else None,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        }
        for m in rows
    ]


# ═══════════════════════════════════════════
# Stale Ideas
# ═══════════════════════════════════════════

@router.get("/stale-ideas")
def stale_ideas(
    threshold: int = Query(30, ge=1),
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Ideas stuck in 'working' status with no thread activity > threshold minutes."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=threshold)

    # Subquery: latest thread activity per idea
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
    rows = db.execute(stmt).all()
    now = datetime.now(timezone.utc)
    return [
        {
            "idea_id": r.id,
            "display_title": (r.title or "Untitled")[:100],
            "minutes_stale": int(
                (now - (r.last_thread or r.updated_at).replace(tzinfo=timezone.utc)).total_seconds() / 60
            ),
        }
        for r in rows
    ]


# ═══════════════════════════════════════════
# Brain Prompts
# ═══════════════════════════════════════════

@router.get("/brain-prompts")
def list_brain_prompts(
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Self-reflection prompts (max 3)."""
    try:
        now = datetime.now(timezone.utc)
        stmt = (
            select(BrainPrompt)
            .where(
                BrainPrompt.resolved_at.is_(None),
                or_(
                    BrainPrompt.dismissed_until.is_(None),
                    BrainPrompt.dismissed_until < now,
                ),
            )
            .order_by(BrainPrompt.created_at.desc())
            .limit(3)
        )
        rows = db.scalars(stmt).all()
        return [
            {
                "id": p.id,
                "prompt_text": p.content,
                "category": p.type,
                "created_at": p.created_at.isoformat() if p.created_at else None,
            }
            for p in rows
        ]
    except Exception as e:
        logger.warning("brain_prompts_error: %s", e)
        return []


@router.post("/brain-prompts/{prompt_id}/teach")
def teach_prompt(
    prompt_id: int,
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Encode a lesson from a brain prompt."""
    prompt = db.get(BrainPrompt, prompt_id)
    if not prompt:
        raise HTTPException(status_code=404, detail="Prompt not found")
    prompt.resolved_at = datetime.now(timezone.utc)
    return {"ok": True, "action": "teach", "prompt_id": prompt_id}


@router.post("/brain-prompts/{prompt_id}/dismiss")
def dismiss_prompt(
    prompt_id: int,
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Snooze a brain prompt for 7 days."""
    prompt = db.get(BrainPrompt, prompt_id)
    if not prompt:
        raise HTTPException(status_code=404, detail="Prompt not found")
    prompt.dismissed_until = datetime.now(timezone.utc) + timedelta(days=7)
    return {"ok": True, "action": "dismiss", "prompt_id": prompt_id}


@router.post("/brain-prompts/{prompt_id}/resolve")
def resolve_prompt(
    prompt_id: int,
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Mark a brain prompt as resolved."""
    prompt = db.get(BrainPrompt, prompt_id)
    if not prompt:
        raise HTTPException(status_code=404, detail="Prompt not found")
    prompt.resolved_at = datetime.now(timezone.utc)
    return {"ok": True, "action": "resolve", "prompt_id": prompt_id}


# ═══════════════════════════════════════════
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
def list_pending_users(
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """List pending (unapproved) users. Owner only."""
    _require_owner(user)
    stmt = (
        select(User)
        .where(User.approved == False)  # noqa: E712
        .order_by(User.created_at.asc())
    )
    rows = db.scalars(stmt).all()
    return [
        {
            "id": u.id,
            "name": u.name,
            "email": u.email,
            "created_at": u.created_at.isoformat() if u.created_at else None,
        }
        for u in rows
    ]


@router.post("/admin/users/{user_id}/approve")
def approve_user(
    user_id: str,
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Approve a pending user in the current workspace."""
    org_id = _require_workspace_member(user)
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if str(target.org_id) != org_id:
        raise HTTPException(status_code=404, detail="User not found")
    if target.approved:
        raise HTTPException(status_code=409, detail="User is already approved")
    target.approved = True
    return {"ok": True, "user_id": user_id, "approved": True}


@router.post("/admin/users/{user_id}/reject")
def reject_user(
    user_id: str,
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Reject (delete) a pending user. Owner only."""
    _require_owner(user)
    target = db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if str(target.org_id) != str(user.get("org_id")):
        raise HTTPException(status_code=404, detail="User not found")
    if target.approved:
        raise HTTPException(status_code=409, detail="User is already approved")
    db.delete(target)
    return {"ok": True, "user_id": user_id, "rejected": True}


# ═══════════════════════════════════════════
# Search
# ═══════════════════════════════════════════

@router.get("/search")
def global_search(
    q: str = Query(..., min_length=2),
    db: Session = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Full-text search across memories and skills."""
    pattern = f"%{q}%"

    # Memories
    mem_stmt = (
        select(Memory)
        .where(
            Memory.content.ilike(pattern),
            or_(Memory.archived == False, Memory.archived.is_(None)),  # noqa: E712
        )
        .order_by(Memory.salience.desc())
        .limit(10)
    )
    memories = db.scalars(mem_stmt).all()

    # Skills
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
    skills = db.scalars(skill_stmt).all()

    return {
        "memories": [
            {
                "id": m.id,
                "content": (m.content or "")[:200],
                "type": m.memory_type,
                "salience": _safe_float(m.salience),
            }
            for m in memories
        ],
        "skills": [
            {
                "id": s.id,
                "name": s.name,
                "description": (s.description or "")[:200],
                "maturity": s.maturity if hasattr(s, "maturity") else None,
            }
            for s in skills
        ],
    }
