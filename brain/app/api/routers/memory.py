"""Memory router — graph, search, CRUD."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from brain.app.api.auth import get_current_user
from brain.app.api.deps import rate_limit
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
from brain.platform.db.models.reconstructive_memory import MemoryNode
from brain.platform.db.repositories.unit_of_work import UnitOfWork

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
                context=_viewer_context(user),
            )
        return await uow.memories.get_graph_data(
            limit=limit,
            context=_viewer_context(user),
        )


@router.get("/graph-similarity", response_model=SimilarityGraphResponse)
async def get_graph_with_similarity(
    limit: Annotated[int | None, Query(ge=25, le=500)] = None,
    top_k: Annotated[int, Query(ge=1, le=10)] = 5,
    threshold: Annotated[float, Query(ge=0.0, le=1.0)] = 0.40,
    user: dict[str, Any] = Depends(get_current_user),
):
    """Return memory graph with pgvector cosine-similarity edges for proximity layout."""
    context = _viewer_context(user)
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
        return await uow.memories.search_visible(q, _viewer_context(user))


@router.get("/stale", response_model=list[MemoryRead])
async def list_stale(
    user: dict[str, Any] = Depends(get_current_user),
):
    async with UnitOfWork() as uow:
        return await uow.memories.list_stale_visible(_viewer_context(user))


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
    context = _viewer_context(user)
    async with UnitOfWork() as uow:
        return await uow.memories.list_org_memories(context, limit=limit, offset=offset)


@router.get("/duplicate-candidates")
async def list_duplicate_candidates(
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
            return await uow.memories.get_or_raise_visible(memory_id, _viewer_context(user))
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
                context=_viewer_context(user),
            )
        except LookupError:
            raise HTTPException(status_code=404, detail="Memory not found")


@router.post("/{memory_id}/truth/review", response_model=MemoryTruthSnapshot)
async def review_memory_truth(
    memory_id: int,
    body: MemoryTruthReviewRequest,
    user: dict[str, Any] = Depends(get_current_user),
):
    """Apply a human truth-maintenance review with required evidence/confidence."""
    async with UnitOfWork() as uow:
        try:
            await uow.memories.get_or_raise_visible(memory_id, _viewer_context(user))
        except LookupError:
            raise HTTPException(status_code=404, detail="Memory not found")
        node = await uow.session.get(MemoryNode, memory_id)
        if node is None:
            raise HTTPException(status_code=404, detail="Memory not found")
        try:
            _apply_truth_review_action(node, body, reviewer_id=user.get("id"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        await uow.session.flush()
        return await uow.memories.get_truth_snapshot(memory_id, include_records=True, context=_viewer_context(user))


@router.get("/{memory_id}/neighborhood", response_model=list[EdgeRead])
async def get_neighborhood(
    memory_id: int,
    user: dict[str, Any] = Depends(get_current_user),
):
    async with UnitOfWork() as uow:
        try:
            return await uow.edges.neighborhood(memory_id, context=_viewer_context(user))
        except LookupError:
            raise HTTPException(status_code=404, detail="Memory not found")


@router.post("/", response_model=MemoryRead, status_code=201)
async def create_memory(
    body: MemoryCreate,
    user: dict[str, Any] = Depends(get_current_user),
):
    context = _write_context(user, source="api_memory", confidence=0.5, evidence={"route": "POST /api/memory"})
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
            await uow.memories.get_or_raise_visible(memory_id, _viewer_context(user))
        except LookupError:
            raise HTTPException(status_code=404, detail="Memory not found")
        node = await uow.session.get(MemoryNode, memory_id)
        if node is None:
            raise HTTPException(status_code=404, detail="Memory not found")
        updates = body.model_dump(exclude_unset=True)
        if "visibility" in updates:
            updates["visibility"] = _validate_visibility_update(updates["visibility"], user)
        if "content" in updates and updates["content"] is not None:
            node.text = updates["content"]
            node.canonical_label = updates["content"][:240]
        if "scope" in updates and updates["scope"] is not None:
            node.scope_key = str(updates["scope"]).strip() or node.scope_key
        if "visibility" in updates and updates["visibility"] is not None:
            node.visibility = updates["visibility"]
        await uow.session.flush()
        return await uow.memories.get(memory_id)


# ── Action endpoints ─────────────────────────────────────────────────────


@router.post("/{memory_id}/confirm", response_model=MemoryRead)
async def confirm_memory(
    memory_id: int,
    user: dict[str, Any] = Depends(get_current_user),
):
    """Increment salience by 1 and add 'confirmed' tag."""
    async with UnitOfWork() as uow:
        try:
            await uow.memories.get_or_raise_visible(memory_id, _viewer_context(user))
        except LookupError:
            raise HTTPException(status_code=404, detail="Memory not found")
        node = await uow.session.get(MemoryNode, memory_id)
        if node is None:
            raise HTTPException(status_code=404, detail="Memory not found")
        node.confidence = min(float(node.confidence or 0.0) + 0.1, 1.0)
        node.truth_status = "active"
        await uow.session.flush()
        return await uow.memories.get(memory_id)


@router.post("/{memory_id}/flag", response_model=MemoryRead)
async def flag_memory(
    memory_id: int,
    user: dict[str, Any] = Depends(get_current_user),
):
    """Add 'needs_review' tag to the memory."""
    async with UnitOfWork() as uow:
        try:
            await uow.memories.get_or_raise_visible(memory_id, _viewer_context(user))
        except LookupError:
            raise HTTPException(status_code=404, detail="Memory not found")
        node = await uow.session.get(MemoryNode, memory_id)
        if node is None:
            raise HTTPException(status_code=404, detail="Memory not found")
        node.truth_status = "tentative"
        node.freshness_status = "unknown"
        await uow.session.flush()
        return await uow.memories.get(memory_id)


@router.post("/{memory_id}/promote", response_model=MemoryRead)
async def promote_memory(
    memory_id: int,
    body: MemoryPromote,
    user: dict[str, Any] = Depends(get_current_user),
):
    """Change visibility of a memory (e.g. private -> team -> org)."""
    async with UnitOfWork() as uow:
        try:
            await uow.memories.get_or_raise_visible(memory_id, _viewer_context(user))
        except LookupError:
            raise HTTPException(status_code=404, detail="Memory not found")
        node = await uow.session.get(MemoryNode, memory_id)
        if node is None:
            raise HTTPException(status_code=404, detail="Memory not found")
        node.visibility = _validate_visibility_update(body.visibility, user)
        await uow.session.flush()
        return await uow.memories.get(memory_id)


def _apply_truth_review_action(
    node: MemoryNode,
    body: MemoryTruthReviewRequest,
    *,
    reviewer_id: str | None,
) -> None:
    del reviewer_id
    if not body.evidence:
        raise ValueError("truth review requires evidence")
    if not body.rationale:
        raise ValueError("truth review requires rationale")
    to_kind = str(body.to_tier or node.content_kind or "fact").strip().lower()
    now = datetime.now(timezone.utc)

    if body.action == "promote":
        node.content_kind = to_kind
        node.truth_status = "active"
        node.confidence = body.confidence
        node.freshness_status = "fresh"
        node.valid_from = node.valid_from or now
        node.valid_until = body.valid_until
        node.archived_at = None
    elif body.action == "demote":
        node.content_kind = to_kind
        node.truth_status = body.truth_status or "tentative"
        node.confidence = min(body.confidence, 0.5)
        node.freshness_status = "stale"
    elif body.action == "quarantine":
        node.truth_status = "quarantined"
        node.confidence = min(body.confidence, 0.4)
        node.freshness_status = "stale"
    else:
        truth_status = str(body.truth_status or node.truth_status or "tentative").strip().lower()
        if truth_status not in {"unknown", "tentative", "active", "reviewed", "superseded", "expired", "quarantined"}:
            raise ValueError("invalid truth_status")
        node.truth_status = truth_status
        node.confidence = body.confidence
        node.valid_from = node.valid_from or now
        node.valid_until = body.valid_until


def _validate_visibility_update(value: str | None, user: dict[str, Any]) -> str:
    visibility = str(value or "").strip().lower()
    if visibility not in {"private", "team", "org"}:
        raise HTTPException(status_code=400, detail="Invalid visibility value")
    if visibility in {"team", "org"} and not user.get("org_id"):
        raise HTTPException(status_code=400, detail="Organization context required")
    return visibility


def _viewer_context(user: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        user_id=user.get("id"),
        org_id=user.get("org_id"),
        allow_global=False,
    )


def _write_context(
    user: dict[str, Any],
    *,
    source: str,
    confidence: float,
    evidence: dict[str, Any],
    visibility: str = "private",
) -> SimpleNamespace:
    return SimpleNamespace(
        user_id=user["id"],
        org_id=user.get("org_id"),
        visibility=visibility,
        source=source,
        confidence=confidence,
        evidence=evidence,
        source_ref=lambda: None,
        source_session=lambda: None,
    )
