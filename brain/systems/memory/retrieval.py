#!/usr/bin/env python3
"""
Retrieval Service — query preprocessing, feedback tracking, and quality stats.

Improves retrieval quality by:
1. Preprocessing raw messages into better search queries before embedding
2. Tracking which retrieved memories were actually useful (feedback loop)
3. Providing stats to tune retrieval parameters over time
"""

import json
import logging
import os
import sys

from sqlalchemy import text

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))
from brain.platform.db.repositories.unit_of_work import UnitOfWork
logger = logging.getLogger(__name__)


def preprocess_query(raw_message: str) -> str:
    """Extract the actual information need from a raw message before embedding.

    Raw messages often contain emotional language, filler, or implicit references
    that don't embed well. This extracts the semantic core.

    Examples:
        "this is still broken" → "recurring failure patterns, debugging history"
        "why does this keep happening" → "repeated issues, root cause patterns"
        "perfect, that's exactly what I wanted" → "successful approach, positive outcome"
    """
    try:
        from brain.jobs.pipelines.agent_cli import call_agent, extract_json

        prompt = f"""Extract the information need from this message for a memory retrieval system.
The system stores: lessons learned, engineering decisions, emotional patterns, skill history, and work episodes.

Message: "{raw_message[:300]}"

What topics/concepts should we search for? Return ONLY a JSON object:
{{"query": "concise search terms and concepts, comma-separated", "intent": "recall|context|emotional|skill"}}"""

        result = call_agent(
            session_id="query-preprocess",
            message=prompt,
            thinking="off",
        )

        if result["success"]:
            data = extract_json(result["text"])
            if data and "query" in data:
                return data["query"]
    except Exception as e:
        logger.debug(f"Query preprocessing failed: {e}")

    # Fallback: return raw message
    return raw_message


def mark_relevant(retrieval_log_id: int, relevant: bool) -> bool:
    """Mark a retrieval log entry as relevant or not.

    Args:
        retrieval_log_id: ID from the retrieval_log table
        relevant: True if the retrieved memory was useful

    Returns:
        True if the update succeeded
    """
    feedback = "hit" if relevant else "miss"
    try:
        with UnitOfWork() as uow:
            result = uow.session.execute(text(
                "UPDATE retrieval_log SET was_relevant = :relevant, feedback = :feedback "
                "WHERE id = :id RETURNING id"
            ), {"relevant": relevant, "feedback": feedback, "id": retrieval_log_id})
            row = result.mappings().first()
            return row is not None
    except Exception as e:
        logger.error(f"Failed to mark retrieval {retrieval_log_id}: {e}")
        return False


def get_retrieval_stats(days: int = 7) -> dict:
    """Get retrieval quality statistics.

    Returns:
        {
            total: int,
            with_feedback: int,
            hits: int,
            misses: int,
            hit_rate: float (0-1),
            avg_top_score: float,
            avg_results_returned: float,
        }
    """
    with UnitOfWork() as uow:
        result = uow.session.execute(text("""
            SELECT
                COUNT(*) as total,
                COUNT(*) FILTER (WHERE feedback IS NOT NULL) as with_feedback,
                COUNT(*) FILTER (WHERE feedback = 'hit') as hits,
                COUNT(*) FILTER (WHERE feedback = 'miss') as misses,
                ROUND(AVG(top_score)::numeric, 3) as avg_top_score,
                ROUND(AVG(results_returned)::numeric, 1) as avg_results
            FROM retrieval_log
            WHERE timestamp >= NOW() - INTERVAL '1 day' * :days
        """), {"days": days})
        row = result.mappings().first()

        total = row["total"] or 0
        with_feedback = row["with_feedback"] or 0
        hits = row["hits"] or 0
        misses = row["misses"] or 0

        return {
            "total": total,
            "with_feedback": with_feedback,
            "hits": hits,
            "misses": misses,
            "hit_rate": round(hits / max(with_feedback, 1), 3),
            "no_feedback_count": total - with_feedback,
            "avg_top_score": float(row["avg_top_score"]) if row["avg_top_score"] else 0.0,
            "avg_results_returned": float(row["avg_results"]) if row["avg_results"] else 0.0,
        }
