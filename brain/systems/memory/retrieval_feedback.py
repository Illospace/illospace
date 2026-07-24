#!/usr/bin/env python3
"""Retrieval feedback loop — adjusts memory salience based on retrieval usefulness.

Issue #5: When memories are retrieved, feedback (hit/miss/partial) now updates
the memory's salience score, creating a reinforcement loop where useful memories
rise and stale ones sink.

Rules:
- hit:     salience += 0.5 (capped at 10)
- miss:    salience -= 0.3 (floored at 1)
- partial: logged only, no salience change

Automatic feedback (memory-DAG):
- Success:  salience += 0.05 per memory
- Failure:  salience -= 0.03 per memory
- Explicit: +0.3 (positive) / -0.2 (negative)
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.repositories.reconstructive_memory import ReconstructionRepository
from brain.platform.db.repositories.system import RetrievalLogRepository
from brain.platform.db.repositories.unit_of_work import UnitOfWork

logger = logging.getLogger(__name__)

# Legacy manual feedback constants
BOOST_HIT = 0.5
PENALTY_MISS = 0.3
SALIENCE_CAP = 10.0
SALIENCE_FLOOR = 1.0

# Automatic feedback constants (memory-DAG)
BOOST_SUCCESS = 0.05
PENALTY_FAILURE = 0.03
BOOST_EXPLICIT = 0.3
PENALTY_EXPLICIT = 0.2


async def record_reconstruction_retrieval_feedback(
    session: AsyncSession,
    *,
    query: str,
    evidence: list[dict[str, Any]],
    reconstruction_run_id: int,
    org_id: str | None,
) -> dict[str, Any]:
    """Persist the one reconstructive retrieval/feedback attribution path.

    V1 proxy: evidence selected into a completed evidence pack is considered
    relevant. This records what was returned to the agent, not a verified final-
    answer citation; the explicit proxy label keeps that distinction queryable.
    """

    normalized_evidence = [item for item in evidence if isinstance(item, dict)]
    top_item = normalized_evidence[0] if normalized_evidence else {}
    top_result_id = _feedback_node_id(top_item)
    top_score = max((_feedback_score(item) for item in normalized_evidence), default=0.0)
    was_relevant = bool(normalized_evidence)
    retrieval_log = await RetrievalLogRepository(session).create_reconstruction_log(
        query_text=query[:500],
        results_returned=len(normalized_evidence),
        top_result_id=top_result_id,
        top_score=round(top_score, 4),
        was_relevant=was_relevant,
        feedback="hit" if was_relevant else "miss",
        org_id=org_id,
    )

    feedback_repo = ReconstructionRepository(session)
    feedback_ids: list[int] = []
    if normalized_evidence:
        for rank, item in enumerate(normalized_evidence):
            feedback = await feedback_repo.add_feedback(
                reconstruction_run_id=reconstruction_run_id,
                signal_kind="selected_evidence_proxy",
                target_node_id=_feedback_node_id(item),
                details={
                    "proxy": "selected_into_completed_evidence_pack",
                    "retrieval_log_id": retrieval_log.id,
                    "rank": rank,
                    "confidence": _feedback_score(item),
                },
            )
            feedback_ids.append(feedback.id)
    else:
        feedback = await feedback_repo.add_feedback(
            reconstruction_run_id=reconstruction_run_id,
            signal_kind="no_evidence_proxy",
            details={
                "proxy": "completed_evidence_pack_was_empty",
                "retrieval_log_id": retrieval_log.id,
            },
        )
        feedback_ids.append(feedback.id)

    return {
        "retrieval_log_id": retrieval_log.id,
        "was_relevant": was_relevant,
        "feedback_ids": feedback_ids,
    }


def _feedback_node_id(item: dict[str, Any]) -> int | None:
    value = item.get("node_id", item.get("id"))
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _feedback_score(item: dict[str, Any]) -> float:
    value = item.get("confidence", item.get("similarity", 0.0))
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


async def apply_retrieval_feedback(log_id: int, feedback: str, *, cur=None) -> dict:
    """Apply feedback to a retrieval_log entry and adjust memory salience.

    Args:
        log_id: retrieval_log row id
        feedback: 'hit', 'miss', or 'partial'
        cur: deprecated, ignored — uses UnitOfWork internally

    Returns:
        dict with log_id, feedback, memory_id, old_salience, new_salience
    """
    async def _do(uow):
        was_relevant = feedback == "hit"
        await uow.session.execute(text("""
            UPDATE retrieval_log SET was_relevant = :was_relevant, feedback = :feedback WHERE id = :id
        """), {"was_relevant": was_relevant, "feedback": feedback, "id": log_id})

        # Get the memory that was retrieved
        result = await uow.session.execute(text(
            "SELECT top_result_id FROM retrieval_log WHERE id = :id"
        ), {"id": log_id})
        row = result.mappings().first()
        if not row or not row["top_result_id"]:
            return {"log_id": log_id, "feedback": feedback, "memory_id": None,
                    "old_salience": None, "new_salience": None}

        memory_id = row["top_result_id"]

        if feedback == "partial":
            return {"log_id": log_id, "feedback": feedback, "memory_id": memory_id,
                    "old_salience": None, "new_salience": None}

        result = await uow.session.execute(text(
            "SELECT confidence * 10 AS salience FROM memory_nodes WHERE id = :id"
        ), {"id": memory_id})
        mem = result.mappings().first()
        if not mem:
            return {"log_id": log_id, "feedback": feedback, "memory_id": memory_id,
                    "old_salience": None, "new_salience": None}

        old_salience = float(mem["salience"])

        if feedback == "hit":
            new_salience = min(old_salience + BOOST_HIT, SALIENCE_CAP)
        elif feedback == "miss":
            new_salience = max(old_salience - PENALTY_MISS, SALIENCE_FLOOR)
        else:
            new_salience = old_salience

        await uow.session.execute(text(
            "UPDATE memory_nodes SET confidence = :confidence WHERE id = :id"
        ), {"confidence": max(0.0, min(1.0, new_salience / 10.0)), "id": memory_id})

        return {"log_id": log_id, "feedback": feedback, "memory_id": memory_id,
                "old_salience": old_salience, "new_salience": new_salience}

    async with UnitOfWork() as uow:
        return await _do(uow)


async def analyze_missed_memories(*, cur=None, min_misses: int = 3, days: int = 30) -> list[dict]:
    """Find memories that are consistently missed in retrievals.

    Args:
        cur: deprecated, ignored — uses UnitOfWork internally
        min_misses: minimum miss count threshold
        days: lookback window

    Returns list of {memory_id, content, miss_count, hit_count, salience}.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    async with UnitOfWork() as uow:
        result = await uow.session.execute(text("""
            SELECT rl.top_result_id as memory_id,
                   COALESCE(m.text, m.canonical_label) AS content,
                   m.confidence * 10 AS salience,
                   COUNT(*) FILTER (WHERE rl.feedback = 'miss') as miss_count,
                   COUNT(*) FILTER (WHERE rl.feedback = 'hit') as hit_count
            FROM retrieval_log rl
            JOIN memory_nodes m ON m.id = rl.top_result_id
            WHERE rl.timestamp >= :cutoff
              AND rl.top_result_id IS NOT NULL
              AND rl.feedback IS NOT NULL
            GROUP BY rl.top_result_id, COALESCE(m.text, m.canonical_label), m.confidence
            HAVING COUNT(*) FILTER (WHERE rl.feedback = 'miss') >= :min_misses
            ORDER BY miss_count DESC
        """), {"cutoff": cutoff, "min_misses": min_misses})
        return [dict(r) for r in result.mappings().all()]


# ---------------------------------------------------------------------------
# Automatic feedback (memory-DAG)
# ---------------------------------------------------------------------------


async def apply_auto_feedback(
    memory_ids: list[int],
    pool_tags: list[str],
    *,
    success: bool,
    org_id: str | None = None,
) -> None:
    """Apply automatic salience adjustment to retrieved memories.

    On success each memory receives +BOOST_SUCCESS; on failure -PENALTY_FAILURE.
    Pool outcomes are recorded via RetrievalPoolStatsRepository.
    """
    delta = BOOST_SUCCESS if success else -PENALTY_FAILURE

    async with UnitOfWork() as uow:
        for mid in memory_ids:
            await _adjust_salience_uow(uow, mid, delta)
        for pool_name in pool_tags:
            await _record_pool_outcome_uow(uow, pool_name, hit=success, org_id=org_id)


async def apply_explicit_feedback(
    memory_ids: list[int],
    *,
    positive: bool,
) -> None:
    """Apply explicit (user-driven) salience adjustment.

    Positive feedback: +BOOST_EXPLICIT.  Negative: -PENALTY_EXPLICIT.
    """
    delta = BOOST_EXPLICIT if positive else -PENALTY_EXPLICIT

    async with UnitOfWork() as uow:
        for mid in memory_ids:
            await _adjust_salience_uow(uow, mid, delta)


async def record_attention_usefulness(
    retrieval_decision_id: int,
    *,
    user_id: str | None = None,
    org_id: str | None = None,
    service_retrieval: bool = False,
    item_id: int | None = None,
    summary_id: int | None = None,
    actually_used: bool | None = None,
    cited_in_output: bool | None = None,
    correlated_with_success: bool | None = None,
    lazy_loaded: bool | None = None,
    retry_delta: int | None = None,
    verifier_helped: bool | None = None,
    user_feedback_signal: str | None = None,
) -> bool:
    """Write real usefulness signals back to the attention controller."""
    from brain.systems.memory.attention_controller import AttentionController

    return await AttentionController().record_usefulness(
        retrieval_decision_id=retrieval_decision_id,
        user_id=user_id,
        org_id=org_id,
        service_retrieval=service_retrieval,
        item_id=item_id,
        summary_id=summary_id,
        actually_used=actually_used,
        cited_in_output=cited_in_output,
        correlated_with_success=correlated_with_success,
        lazy_loaded=lazy_loaded,
        retry_delta=retry_delta,
        verifier_helped=verifier_helped,
        user_feedback_signal=user_feedback_signal,
    )


async def record_attention_lazy_load(
    retrieval_decision_id: int,
    *,
    user_id: str | None = None,
    org_id: str | None = None,
    service_retrieval: bool = False,
    item_id: int | None = None,
    summary_id: int | None = None,
) -> bool:
    """Mark a deferred item as lazily loaded through a real follow-up lookup."""
    from brain.systems.memory.attention_controller import AttentionController

    return await AttentionController().record_lazy_load(
        retrieval_decision_id=retrieval_decision_id,
        user_id=user_id,
        org_id=org_id,
        service_retrieval=service_retrieval,
        item_id=item_id,
        summary_id=summary_id,
    )


async def _adjust_salience(memory_id: int, delta: float) -> None:
    """Adjust a single memory's salience using UnitOfWork (standalone)."""
    async with UnitOfWork() as uow:
        await _adjust_salience_uow(uow, memory_id, delta)


async def _adjust_salience_uow(uow, memory_id: int, delta: float) -> None:
    """Adjust a single memory's salience within an existing UnitOfWork."""
    from brain.platform.db.models.reconstructive_memory import MemoryNode

    mem = await uow.session.get(MemoryNode, memory_id)
    if mem is None:
        logger.warning("Memory %d not found for salience adjustment", memory_id)
        return

    old = float(mem.confidence or 0.0) * 10
    adjusted = max(SALIENCE_FLOOR, min(SALIENCE_CAP, old + delta))
    mem.confidence = max(0.0, min(1.0, adjusted / 10.0))


async def _record_pool_outcome_uow(
    uow, pool_name: str, *, hit: bool, org_id: str | None = None
) -> None:
    """Record a pool outcome within an existing UnitOfWork."""
    await uow.pool_stats.record_outcome(pool_name, hit=hit, org_id=org_id)
