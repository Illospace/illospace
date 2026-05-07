"""Cortex Intelligence router — connection detection, emergence, optimization, gravity."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from sqlalchemy import text

from brain.app.api.auth import get_current_user
from brain.app.api.deps import rate_limit
from brain.platform.db.repositories.unit_of_work import UnitOfWork

router = APIRouter(
    prefix="/api/cortex",
    tags=["cortex-intel"],
    dependencies=[Depends(rate_limit)],
)


@router.post("/detect-connections")
async def api_detect_connections(request: Request, user: dict[str, Any] = Depends(get_current_user)):
    from brain.systems.cortex.intelligence import detect_connections, LINK_THRESHOLD
    body = await request.json() if await request.body() else {}
    threshold = body.get('threshold', LINK_THRESHOLD) if body else LINK_THRESHOLD
    result = detect_connections(threshold)
    return result


@router.post("/emerge")
def api_emerge(user: dict[str, Any] = Depends(get_current_user)):
    from brain.jobs.pipelines.cortex_emerge import run_emergence
    result = run_emergence()
    return result


@router.post("/optimize")
def api_optimize(user: dict[str, Any] = Depends(get_current_user)):
    from brain.jobs.pipelines.cortex_optimize import run_optimization
    result = run_optimization()
    return result


@router.get("/intelligence/status")
def api_intel_status(user: dict[str, Any] = Depends(get_current_user)):
    with UnitOfWork() as uow:
        idea_counts = uow.session.execute(
            text(
                """
                SELECT
                    SUM(CASE WHEN embedding IS NOT NULL THEN 1 ELSE 0 END) AS embedded,
                    SUM(CASE WHEN status = 'emerged' THEN 1 ELSE 0 END) AS emerged,
                    SUM(CASE WHEN status = 'stale' THEN 1 ELSE 0 END) AS stale
                FROM ideas
                """
            )
        ).mappings().first()
        auto_connections = uow.session.execute(
            text("SELECT count(*) as c FROM idea_connections WHERE type = 'similarity'")
        ).mappings().first()['c']
    return {
        'embedded_ideas': int((idea_counts or {}).get('embedded') or 0),
        'emerged_ideas': int((idea_counts or {}).get('emerged') or 0),
        'auto_connections': int(auto_connections or 0),
        'stale_ideas': int((idea_counts or {}).get('stale') or 0),
    }


@router.get("/similarity-matrix")
def api_similarity_matrix(user: dict[str, Any] = Depends(get_current_user)):
    from brain.systems.cortex.intelligence import similarity_matrix
    return similarity_matrix()


@router.post("/gravity")
async def gravity_scores(request: Request, user: dict[str, Any] = Depends(get_current_user)):
    from brain.systems.cortex.intelligence import compute_gravity
    data = await request.json() if await request.body() else {}
    scores = compute_gravity(
        query_text=(data.get('query') or '').strip(),
        user_filter=data.get('users') or [],
        status_filter=data.get('statuses') or [],
        focus_idea_id=data.get('idea_id'),
        loaded_idea_ids=data.get('idea_ids') or [],
        current_user_id=user.get("id"),
    )
    return scores
