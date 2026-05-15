"""Cortex Intelligence router — connection detection, emergence, optimization, gravity."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from brain.app.api.auth import get_current_user
from brain.app.api.deps import get_db, rate_limit

router = APIRouter(
    prefix="/api/cortex",
    tags=["cortex-intel"],
    dependencies=[Depends(rate_limit)],
)


@router.post("/detect-connections")
async def api_detect_connections(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from brain.systems.cortex.intelligence import LINK_THRESHOLD, async_detect_connections
    body = await request.json() if await request.body() else {}
    threshold = body.get('threshold', LINK_THRESHOLD) if body else LINK_THRESHOLD
    return await async_detect_connections(db, threshold)


@router.post("/emerge")
async def api_emerge(
    user: dict[str, Any] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from brain.jobs.pipelines.cortex_emerge import async_run_emergence
    return await async_run_emergence(db)


@router.post("/optimize")
async def api_optimize(
    user: dict[str, Any] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from brain.jobs.pipelines.cortex_optimize import async_run_optimization
    return await async_run_optimization(db)


@router.get("/intelligence/status")
async def api_intel_status(
    user: dict[str, Any] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    idea_counts_result = await db.execute(
        text(
            """
            SELECT
                SUM(CASE WHEN embedding IS NOT NULL THEN 1 ELSE 0 END) AS embedded,
                SUM(CASE WHEN status = 'emerged' THEN 1 ELSE 0 END) AS emerged,
                SUM(CASE WHEN status = 'stale' THEN 1 ELSE 0 END) AS stale
            FROM ideas
            """
        )
    )
    idea_counts = idea_counts_result.mappings().first()
    auto_connections_result = await db.execute(
        text("SELECT count(*) as c FROM idea_connections WHERE type = 'similarity'")
    )
    auto_connections = auto_connections_result.mappings().first()['c']
    return {
        'embedded_ideas': int((idea_counts or {}).get('embedded') or 0),
        'emerged_ideas': int((idea_counts or {}).get('emerged') or 0),
        'auto_connections': int(auto_connections or 0),
        'stale_ideas': int((idea_counts or {}).get('stale') or 0),
    }


@router.get("/similarity-matrix")
async def api_similarity_matrix(
    user: dict[str, Any] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from brain.systems.cortex.intelligence import async_similarity_matrix
    return await async_similarity_matrix(db)


@router.post("/gravity")
async def gravity_scores(
    request: Request,
    user: dict[str, Any] = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    from brain.systems.cortex.intelligence import async_compute_gravity
    data = await request.json() if await request.body() else {}
    scores = await async_compute_gravity(
        db,
        query_text=(data.get('query') or '').strip(),
        user_filter=data.get('users') or [],
        status_filter=data.get('statuses') or [],
        focus_idea_id=data.get('idea_id'),
        loaded_idea_ids=data.get('idea_ids') or [],
        current_user_id=user.get("id"),
    )
    return scores
