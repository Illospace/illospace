"""Memory encoding MCP tool implementation."""
from __future__ import annotations

from typing import Any


async def brain_encode_tool(
    content: str,
    memory_type: str = "episode",
    salience: float = 5.0,
    source: str = "agent_run",
    user_id: str | None = None,
    org_id: str | None = None,
    visibility: str = "private",
    conversation_id: str | None = None,
    idea_id: str | None = None,
    run_id: int | str | None = None,
    session_id: str | None = None,
    confidence: float | None = None,
    evidence: dict | None = None,
    *,
    unit_of_work_cls: Any,
    maybe_await: Any,
    write_context_cls: Any,
) -> dict:
    """Encode a new memory into the brain, scoped to the current user."""
    from brain.systems.memory.embedding_service import EmbeddingService, embedding_degradation_reason

    if len(content.strip()) < 20:
        return {"error": "Content too short (min 20 chars)"}

    if not user_id:
        return {"error": "brain_encode requires user context (missing user_id)"}

    if visibility not in ("private", "team", "org"):
        visibility = "private"

    try:
        write_context = write_context_cls(
            user_id=user_id,
            org_id=org_id,
            visibility=visibility,
            source=source,
            conversation_id=conversation_id,
            idea_id=idea_id,
            run_id=run_id,
            session_id=session_id,
            confidence=confidence,
            evidence=evidence or {},
        )
    except ValueError as exc:
        return {"error": str(exc)}

    semantic_emb = None
    degraded_reason = None

    async with unit_of_work_cls() as uow:
        try:
            embedding_service = await EmbeddingService.from_session(uow.session)
            semantic_emb = embedding_service.document(content)
        except Exception as exc:
            degraded_reason = embedding_degradation_reason(exc)

        result = await maybe_await(uow.memories.insert_memory(
            content=content,
            memory_type=memory_type,
            salience=salience,
            semantic_embedding=semantic_emb,
            context=write_context,
            auto_edge=False,
        ))

    if degraded_reason:
        result["warning"] = degraded_reason
        result["embedding_deferred"] = True
    return result
