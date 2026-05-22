"""Memory recall MCP tool implementation."""
from __future__ import annotations

import os
from typing import Any

from brain.platform.db.repositories.memories import MemoryRepository


async def add_attribution(session: Any, memories: list[dict], current_user_id: str) -> list[dict]:
    """Add attribution info to shared memories from other users."""
    try:
        return await MemoryRepository(session).add_attribution(memories, current_user_id)
    except Exception:
        pass
    return memories


async def log_retrieval(
    query: str,
    results: list,
    *,
    unit_of_work_cls: Any,
    maybe_await: Any,
    session_flush: Any,
    logger: Any,
) -> None:
    """Insert a row into retrieval_log for metrics tracking. Non-blocking."""
    try:
        top_score = max((r.get("similarity", 0) for r in results), default=0)
        async with unit_of_work_cls() as uow:
            await maybe_await(uow.retrieval_logs.create(
                query_text=query[:500],
                results_returned=len(results),
                top_score=round(float(top_score), 4),
            ))
            await session_flush(uow.session)
    except Exception as exc:
        logger.debug(f"retrieval_log insert failed (non-critical): {exc}")


async def brain_recall_tool(
    query: str,
    limit: int = 3,
    user_id: str | None = None,
    org_id: str | None = None,
    attention_debug: bool = False,
    expand_lazy_load: bool | None = None,
    service_retrieval: bool = False,
    *,
    unit_of_work_cls: Any,
    maybe_await: Any,
    session_flush: Any,
    visibility_context_cls: Any,
    observe_retrieval_fn: Any,
    attention_controller_cls: Any,
    logger: Any,
) -> dict:
    """Graph-augmented memory search — vector similarity + relationship traversal."""
    from brain.systems.memory.embedding_service import EmbeddingService, embedding_degradation_reason

    visibility_context = visibility_context_cls(
        user_id=user_id,
        org_id=org_id,
        allow_global=service_retrieval or (user_id == "system"),
        principal_type="service" if service_retrieval or user_id == "system" else None,
    )

    query_emb = None
    try:
        async with unit_of_work_cls() as uow:
            embedding_service = await EmbeddingService.from_session(uow.session)
            query_emb = embedding_service.query(query)
            results = await maybe_await(uow.memories.graph_augmented_recall(
                query_embedding=query_emb,
                limit=limit,
                context=visibility_context,
            ))
            memories = []
            for result in results:
                memory = {
                    "id": result["id"],
                    "content": result["content"][:300],
                    "type": result["type"],
                    "tier": result.get("tier", "episodic"),
                    "salience": result.get("salience", 0),
                    "similarity": result.get("similarity", 0),
                    "visibility": result.get("visibility", "private"),
                }
                if result.get("graph_edges"):
                    memory["graph_context"] = result["graph_edges"][:3]
                memories.append(memory)
            if user_id and memories:
                memories = await maybe_await(uow.memories.add_attribution(memories, user_id))
        return await finalize_recall_response(
            query=query,
            memories=memories,
            limit=limit,
            user_id=user_id,
            org_id=org_id,
            attention_debug=attention_debug,
            expand_lazy_load=expand_lazy_load,
            service_retrieval=service_retrieval,
            unit_of_work_cls=unit_of_work_cls,
            maybe_await=maybe_await,
            session_flush=session_flush,
            observe_retrieval_fn=observe_retrieval_fn,
            attention_controller_cls=attention_controller_cls,
            logger=logger,
        )
    except Exception as exc:
        logger.warning(f"Graph recall failed, falling back to vector: {exc}")
        if query_emb is None:
            response = await finalize_recall_response(
                query=query,
                memories=[],
                limit=limit,
                user_id=user_id,
                org_id=org_id,
                attention_debug=attention_debug,
                expand_lazy_load=expand_lazy_load,
                service_retrieval=service_retrieval,
                unit_of_work_cls=unit_of_work_cls,
                maybe_await=maybe_await,
                session_flush=session_flush,
                observe_retrieval_fn=observe_retrieval_fn,
                attention_controller_cls=attention_controller_cls,
                logger=logger,
            )
            response["warning"] = embedding_degradation_reason(exc)
            return response

    async with unit_of_work_cls() as uow:
        memories = await maybe_await(uow.memories.recall_vector(
            query_embedding=query_emb,
            limit=limit,
            context=visibility_context,
        ))
        if user_id and memories:
            memories = await maybe_await(uow.memories.add_attribution(memories, user_id))

    return await finalize_recall_response(
        query=query,
        memories=memories,
        limit=limit,
        user_id=user_id,
        org_id=org_id,
        attention_debug=attention_debug,
        expand_lazy_load=expand_lazy_load,
        service_retrieval=service_retrieval,
        unit_of_work_cls=unit_of_work_cls,
        maybe_await=maybe_await,
        session_flush=session_flush,
        observe_retrieval_fn=observe_retrieval_fn,
        attention_controller_cls=attention_controller_cls,
        logger=logger,
    )


async def finalize_recall_response(
    *,
    query: str,
    memories: list[dict],
    limit: int,
    user_id: str | None,
    org_id: str | None,
    attention_debug: bool,
    expand_lazy_load: bool | None,
    service_retrieval: bool = False,
    unit_of_work_cls: Any,
    maybe_await: Any,
    session_flush: Any,
    observe_retrieval_fn: Any,
    attention_controller_cls: Any,
    logger: Any,
) -> dict:
    await log_retrieval(
        query,
        memories,
        unit_of_work_cls=unit_of_work_cls,
        maybe_await=maybe_await,
        session_flush=session_flush,
        logger=logger,
    )
    service_retrieval = service_retrieval or user_id == "system"
    if not user_id and not org_id and not service_retrieval and not memories:
        return {
            "memories": [],
            "candidate_memories": [],
            "suppressed_memories": [],
            "lazy_load_memories": [],
            "lazy_loaded_memories": [],
            "count": 0,
            "candidate_count": 0,
            "attention_decision": {
                "stage": "brain_recall",
                "retrieval_decision_id": None,
                "selected_count": 0,
                "candidate_count": 0,
                "service_retrieval": False,
                "fallback_used": True,
                "fallback_reason": "missing_user_context",
            },
        }

    attention_decision = await observe_retrieval_fn(
        stage="brain_recall",
        query_text=query,
        candidates=memories,
        user_id=user_id,
        org_id=org_id,
        service_retrieval=service_retrieval,
        preload_budget_tokens=limit * 120,
        lazy_budget_tokens=max(0, limit * 40),
    )
    controller = attention_controller_cls()
    selection = controller.materialize_selection(memories, attention_decision)
    lazy_loaded_memories: list[dict] = []
    retrieval_decision_id = attention_decision.get("retrieval_decision_id")
    should_expand = (
        expand_lazy_load if expand_lazy_load is not None
        else os.getenv("ATTENTION_LAZY_LOAD_ENABLED", "0").strip().lower() not in {"0", "false", "no"}
    )
    if should_expand and selection.lazy_load_eligible and retrieval_decision_id is not None:
        lazy_loaded_memories = await attention_controller_cls().load_lazy_candidates(
            retrieval_decision_id=int(retrieval_decision_id),
            user_id=user_id,
            org_id=org_id,
            service_retrieval=service_retrieval,
            limit=max(0, limit - len(selection.selected)),
        )
    visible_memories = list(selection.selected) + list(lazy_loaded_memories)
    return {
        "memories": visible_memories,
        "candidate_memories": memories,
        "suppressed_memories": selection.suppressed,
        "lazy_load_memories": selection.lazy_load_eligible,
        "lazy_loaded_memories": lazy_loaded_memories,
        "count": len(visible_memories),
        "candidate_count": len(memories),
        "attention_decision": attention_decision,
        **({"attention_explain": attention_controller_cls().explain(attention_decision, memories)} if attention_debug else {}),
    }
