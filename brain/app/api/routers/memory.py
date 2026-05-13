"""Memory router — graph, search, CRUD."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from brain.app.api.auth import get_current_user
from brain.app.api.deps import get_db, rate_limit
from brain.app.api.schemas.memories import (
    EdgeRead,
    GraphResponse,
    MemoryCreate,
    MemoryTruthSnapshot,
    MemoryPromote,
    MemoryRead,
    MemoryTruthReviewRequest,
    MemoryUpdate,
    SimilarityGraphResponse,
)
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.platform.db.models.memory import Memory
from brain.platform.db.repositories.memories import MemoryRepository
from brain.platform.db.repositories.memory_write_context import MemoryWriteContext
from brain.platform.db.repositories.memory_visibility import (
    MemoryVisibilityContext,
    normalize_memory_visibility,
)
from brain.systems.memory.truth_maintenance import (
    async_record_memory_review,
    build_demotion_truth_fields,
    validate_truth_action_context,
)

router = APIRouter(
    prefix="/api/memory",
    tags=["memory"],
    dependencies=[Depends(rate_limit)],
)


@router.get("/graph", response_model=GraphResponse)
async def get_graph(
    limit: Annotated[int | None, Query(ge=25, le=500)] = None,
    user: dict[str, Any] = Depends(get_current_user),
):
    async with UnitOfWork() as uow:
        if limit is None:
            return await uow.memories.get_graph_data(
                context=MemoryVisibilityContext.from_user(user),
            )
        return await uow.memories.get_graph_data(
            limit=limit,
            context=MemoryVisibilityContext.from_user(user),
        )


@router.get("/graph-similarity", response_model=SimilarityGraphResponse)
async def get_graph_with_similarity(
    limit: Annotated[int | None, Query(ge=25, le=500)] = None,
    top_k: Annotated[int, Query(ge=1, le=10)] = 5,
    threshold: Annotated[float, Query(ge=0.0, le=1.0)] = 0.40,
    user: dict[str, Any] = Depends(get_current_user),
):
    """Return memory graph with pgvector cosine-similarity edges for proximity layout."""
    context = MemoryVisibilityContext.from_user(user)
    async with UnitOfWork() as uow:
        if limit is None:
            data = await uow.memories.get_graph_data(context=context)
        else:
            data = await uow.memories.get_graph_data(limit=limit, context=context)
        try:
            if limit is None:
                similarity_edges = await uow.memories.get_similarity_edges(
                    top_k=top_k,
                    threshold=threshold,
                    context=context,
                )
            else:
                similarity_edges = await uow.memories.get_similarity_edges(
                    limit=limit,
                    top_k=top_k,
                    threshold=threshold,
                    context=context,
                )
        except Exception:
            # Graceful fallback if embeddings unavailable
            similarity_edges = []
        return {**data, "similarity_edges": similarity_edges}


@router.get("/search", response_model=list[MemoryRead])
async def search_memories(
    q: str,
    user: dict[str, Any] = Depends(get_current_user),
):
    async with UnitOfWork() as uow:
        return await uow.memories.search_visible(q, MemoryVisibilityContext.from_user(user))


@router.get("/stale", response_model=list[MemoryRead])
async def list_stale(
    user: dict[str, Any] = Depends(get_current_user),
):
    async with UnitOfWork() as uow:
        return await uow.memories.list_stale_visible(MemoryVisibilityContext.from_user(user))


@router.get("/org-memories", response_model=list[MemoryRead])
async def list_org_memories(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: dict[str, Any] = Depends(get_current_user),
):
    """List org-scoped shared memories."""
    org_id = user.get("org_id")
    if not org_id:
        return []
    context = MemoryVisibilityContext.from_user(user)
    async with UnitOfWork() as uow:
        return await uow.memories.list_org_memories(context, limit=limit, offset=offset)


@router.get("/duplicate-candidates")
def list_duplicate_candidates(
    since_hours: int = Query(48, ge=1, le=720),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Return near-duplicate memory pairs.

    TODO: implement vector similarity comparison using pgvector
    cosine distance on the embedding column.  For now returns an
    empty list so the frontend endpoint is wired up.
    """
    return []


@router.get("/{memory_id}", response_model=MemoryRead)
async def get_memory(
    memory_id: int,
    user: dict[str, Any] = Depends(get_current_user),
):
    async with UnitOfWork() as uow:
        try:
            return await uow.memories.get_or_raise_visible(memory_id, MemoryVisibilityContext.from_user(user))
        except LookupError:
            raise HTTPException(status_code=404, detail="Memory not found")


@router.get("/{memory_id}/truth", response_model=MemoryTruthSnapshot)
async def get_memory_truth(
    memory_id: int,
    include_records: bool = Query(False),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Return a read-only truth-maintenance snapshot for a memory."""
    async with UnitOfWork() as uow:
        try:
            return await uow.memories.get_truth_snapshot(
                memory_id,
                include_records=include_records,
                context=MemoryVisibilityContext.from_user(user),
            )
        except LookupError:
            raise HTTPException(status_code=404, detail="Memory not found")


@router.post("/{memory_id}/truth/review", response_model=MemoryTruthSnapshot)
async def review_memory_truth(
    memory_id: int,
    body: MemoryTruthReviewRequest,
    db: AsyncSession = Depends(get_db),
    user: dict[str, Any] = Depends(get_current_user),
):
    """Apply a human truth-maintenance review with required evidence/confidence."""
    repo = MemoryRepository(db)
    context = MemoryVisibilityContext.from_user(user)
    try:
        mem = await repo.a_get_or_raise_visible(memory_id, context)
    except LookupError:
        raise HTTPException(status_code=404, detail="Memory not found")

    contradictions = await repo.a_list_contradictions(memory_id)
    open_contradiction_count = sum(
        1
        for contradiction in contradictions
        if str(getattr(contradiction, "status", "open")).lower()
        not in {"resolved", "closed", "dismissed", "accepted"}
    )
    try:
        await _apply_truth_review_action(
            db,
            mem,
            body,
            reviewer_id=user.get("id"),
            open_contradiction_count=open_contradiction_count,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    await db.flush()
    return await repo.a_get_truth_snapshot(memory_id, include_records=True, context=context)


@router.get("/{memory_id}/neighborhood", response_model=list[EdgeRead])
async def get_neighborhood(
    memory_id: int,
    user: dict[str, Any] = Depends(get_current_user),
):
    async with UnitOfWork() as uow:
        try:
            return await uow.edges.neighborhood(memory_id, context=MemoryVisibilityContext.from_user(user))
        except LookupError:
            raise HTTPException(status_code=404, detail="Memory not found")


@router.post("/", response_model=MemoryRead, status_code=201)
async def create_memory(
    body: MemoryCreate,
    user: dict[str, Any] = Depends(get_current_user),
):
    context = MemoryWriteContext(
        user_id=user["id"],
        org_id=user.get("org_id"),
        visibility="private",
        source="api_memory",
        confidence=0.5,
        evidence={"route": "POST /api/memory"},
    )
    async with UnitOfWork() as uow:
        result = await uow.memories.insert_memory(
            content=body.content,
            memory_type=body.memory_type,
            tags=body.tags or [],
            context=context,
            auto_edge=False,
        )
        await uow.session.flush()
        return await uow.memories.get(result["id"])


@router.patch("/{memory_id}", response_model=MemoryRead)
async def update_memory(
    memory_id: int,
    body: MemoryUpdate,
    user: dict[str, Any] = Depends(get_current_user),
):
    async with UnitOfWork() as uow:
        try:
            mem = await uow.memories.get_or_raise_visible(memory_id, MemoryVisibilityContext.from_user(user))
        except LookupError:
            raise HTTPException(status_code=404, detail="Memory not found")
        updates = body.model_dump(exclude_unset=True)
        if "visibility" in updates:
            updates["visibility"] = _validate_visibility_update(updates["visibility"], user)
        for key, value in updates.items():
            setattr(mem, key, value)
        await uow.session.flush()
        return mem


# ── Action endpoints ─────────────────────────────────────────────────────


@router.post("/{memory_id}/confirm", response_model=MemoryRead)
async def confirm_memory(
    memory_id: int,
    user: dict[str, Any] = Depends(get_current_user),
):
    """Increment salience by 1 and add 'confirmed' tag."""
    async with UnitOfWork() as uow:
        try:
            mem = await uow.memories.get_or_raise_visible(memory_id, MemoryVisibilityContext.from_user(user))
        except LookupError:
            raise HTTPException(status_code=404, detail="Memory not found")
        mem.salience = min((mem.salience or 0) + 1, 10)
        tags = list(mem.tags or [])
        if "confirmed" not in tags:
            tags.append("confirmed")
        mem.tags = tags
        await uow.session.flush()
        return mem


@router.post("/{memory_id}/flag", response_model=MemoryRead)
async def flag_memory(
    memory_id: int,
    user: dict[str, Any] = Depends(get_current_user),
):
    """Add 'needs_review' tag to the memory."""
    async with UnitOfWork() as uow:
        try:
            mem = await uow.memories.get_or_raise_visible(memory_id, MemoryVisibilityContext.from_user(user))
        except LookupError:
            raise HTTPException(status_code=404, detail="Memory not found")
        tags = list(mem.tags or [])
        if "needs_review" not in tags:
            tags.append("needs_review")
        mem.tags = tags
        await uow.session.flush()
        return mem


@router.post("/{memory_id}/promote", response_model=MemoryRead)
async def promote_memory(
    memory_id: int,
    body: MemoryPromote,
    user: dict[str, Any] = Depends(get_current_user),
):
    """Change visibility of a memory (e.g. private -> team -> org)."""
    async with UnitOfWork() as uow:
        try:
            mem = await uow.memories.get_or_raise_visible(memory_id, MemoryVisibilityContext.from_user(user))
        except LookupError:
            raise HTTPException(status_code=404, detail="Memory not found")
        mem.visibility = _validate_visibility_update(body.visibility, user)
        await uow.session.flush()
        return mem


async def _apply_truth_review_action(
    db: AsyncSession,
    mem: Memory,
    body: MemoryTruthReviewRequest,
    *,
    reviewer_id: str | None,
    open_contradiction_count: int = 0,
) -> None:
    action_context = validate_truth_action_context(
        action=body.action,
        evidence=body.evidence,
        confidence=body.confidence,
    )
    evidence = dict(action_context["evidence"])
    evidence.update({
        "api_route": "POST /api/memory/{memory_id}/truth/review",
        "human_review": True,
        "open_contradiction_count": open_contradiction_count,
    })
    from_tier = str(getattr(mem, "memory_tier", None) or "episodic")
    to_tier = str(body.to_tier or from_tier).strip().lower()
    now = datetime.now(timezone.utc)

    if body.action == "promote":
        _validate_tier(to_tier)
        if open_contradiction_count > 0 and body.confidence < 0.8:
            raise ValueError("promoting a contradicted memory requires confidence >= 0.8")
        mem.memory_tier = to_tier
        mem.truth_status = "reviewed"
        mem.review_status = "reviewed"
        mem.confidence = body.confidence
        mem.freshness_score = max(float(getattr(mem, "freshness_score", 0.5) or 0.5), body.confidence)
        mem.valid_from = getattr(mem, "valid_from", None) or now
        mem.valid_until = body.valid_until
        mem.reviewed_at = now
        mem.reviewed_by = reviewer_id
        mem.demoted_at = None
        mem.demotion_reason = None
    elif body.action == "demote":
        _validate_tier(to_tier)
        mem.memory_tier = to_tier
        mem.truth_status = body.truth_status or "tentative"
        mem.review_status = "reviewed"
        mem.confidence = min(body.confidence, 0.5)
        mem.freshness_score = min(float(getattr(mem, "freshness_score", 0.5) or 0.5), 0.4)
        mem.reviewed_at = now
        mem.reviewed_by = reviewer_id
        mem.demoted_at = now
        mem.demotion_reason = body.rationale
    elif body.action == "quarantine":
        fields = build_demotion_truth_fields(
            reason=body.rationale,
            confidence=body.confidence,
            evidence=evidence,
            reviewed_by=reviewer_id,
            quarantine=True,
        )
        for key, value in fields.items():
            setattr(mem, key, value)
    else:
        truth_status = str(body.truth_status or getattr(mem, "truth_status", None) or "tentative").strip().lower()
        if truth_status not in {"unknown", "tentative", "reviewed", "superseded", "expired", "quarantined"}:
            raise ValueError("invalid truth_status")
        mem.truth_status = truth_status
        mem.review_status = "reviewed"
        mem.confidence = body.confidence
        mem.valid_from = getattr(mem, "valid_from", None) or now
        mem.valid_until = body.valid_until
        mem.reviewed_at = now
        mem.reviewed_by = reviewer_id

    await async_record_memory_review(
        db,
        memory_id=mem.id,
        action=body.action,
        from_tier=from_tier,
        to_tier=to_tier,
        reviewer_id=reviewer_id,
        rationale=body.rationale,
        evidence=evidence,
        confidence=body.confidence,
    )
    if body.action in {"demote", "quarantine"} or str(getattr(mem, "truth_status", "")).lower() in {
        "superseded",
        "expired",
        "quarantined",
    }:
        from brain.platform.db.repositories.memory_dag import MemorySummaryRepository
        from brain.platform.db.repositories.narratives import NarrativeRepository

        reason = body.rationale or f"memory truth review: {body.action}"
        await MemorySummaryRepository(db).a_mark_stale_for_memory(mem.id, reason)
        await NarrativeRepository(db).a_mark_stale_for_memory(mem.id, reason)


def _validate_tier(value: str) -> None:
    if value not in {"episodic", "semantic", "procedural", "policy"}:
        raise ValueError("invalid memory tier")


def _validate_visibility_update(value: str | None, user: dict[str, Any]) -> str:
    visibility = normalize_memory_visibility(value, fallback="")
    if not visibility:
        raise HTTPException(status_code=400, detail="Invalid visibility value")
    if visibility in {"team", "org"} and not user.get("org_id"):
        raise HTTPException(status_code=400, detail="Organization context required")
    return visibility
