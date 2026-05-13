"""Cross-user divergence detection for org-level nightly cycle.

Analyzes recent memories per-user, computes topic overlap via tag
intersection and Jaccard similarity, and suggests sync meetings
when users are working on related but potentially divergent efforts.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy import text

from brain.platform.db.repositories.unit_of_work import UnitOfWork

logger = logging.getLogger(__name__)


async def detect_divergence(
    target_date: date,
    org_id: str,
    lookback_days: int = 7,
    overlap_threshold: float = 0.3,
) -> list[dict]:
    """Detect topic overlap between users in the same org.
    Returns list of dicts: {user_a, user_b, shared_topics, similarity, suggestion}
    """
    since = target_date - timedelta(days=lookback_days)

    async with UnitOfWork() as uow:
        result = await uow.session.execute(text("""
            SELECT m.user_id, u.name AS user_name,
                   array_agg(DISTINCT unnest_tag) AS topic_tags,
                   string_agg(DISTINCT LEFT(m.content, 100), ' | ') AS content_sample
            FROM memories m
            JOIN users u ON u.id = m.user_id
            CROSS JOIN LATERAL unnest(m.tags) AS unnest_tag
            WHERE m.org_id = :org_id
              AND m.created_at::date >= :since
              AND NOT m.archived
              AND m.tags IS NOT NULL AND array_length(m.tags, 1) > 0
            GROUP BY m.user_id, u.name
        """), {"org_id": org_id, "since": since})
        user_topics = [dict(r) for r in result.mappings().all()]

    if len(user_topics) < 2:
        return []

    overlaps = []
    for i, a in enumerate(user_topics):
        for b in user_topics[i + 1:]:
            tags_a = set(a["topic_tags"] or [])
            tags_b = set(b["topic_tags"] or [])
            if not tags_a or not tags_b:
                continue
            shared = tags_a & tags_b
            union = tags_a | tags_b
            similarity = len(shared) / len(union) if union else 0

            if similarity >= overlap_threshold and len(shared) >= 2:
                overlaps.append({
                    "user_a": a["user_name"],
                    "user_a_id": a["user_id"],
                    "user_b": b["user_name"],
                    "user_b_id": b["user_id"],
                    "shared_topics": sorted(shared),
                    "unique_to_a": sorted(tags_a - tags_b),
                    "unique_to_b": sorted(tags_b - tags_a),
                    "similarity": round(similarity, 3),
                    "context_a": a["content_sample"][:200],
                    "context_b": b["content_sample"][:200],
                    "suggestion": format_sync_suggestion(
                        a["user_name"], b["user_name"], sorted(shared), similarity,
                    ),
                })

    overlaps.sort(key=lambda x: x["similarity"], reverse=True)
    return overlaps


def format_sync_suggestion(
    user_a: str,
    user_b: str,
    shared_topics: list[str],
    similarity: float,
) -> str:
    """Format a human-readable sync suggestion."""
    topics_str = ", ".join(shared_topics[:5])
    intensity = "strongly" if similarity > 0.6 else "partially"
    return (
        f"{user_a} and {user_b} are {intensity} overlapping on: {topics_str}. "
        f"Consider a sync to align efforts and avoid duplication."
    )


async def store_divergence_results(
    target_date: date,
    org_id: str,
    overlaps: list[dict],
) -> None:
    """Store divergence findings as an org-level memory."""
    if not overlaps:
        return

    summary_lines = [f"## Divergence Report — {target_date}\n"]
    for o in overlaps:
        summary_lines.append(f"- {o['suggestion']}")

    content = "\n".join(summary_lines)

    from brain.systems.memory.embeddings import embed_document, vec_to_pg
    embedding = embed_document(content)

    async with UnitOfWork() as uow:
        await uow.session.execute(text("""
            INSERT INTO memories (content, memory_type, semantic_embedding,
                salience, tags, source, org_id, user_id, scope)
            VALUES (:content, 'meta', :embedding, 7.0, :tags, 'nightly:divergence', :org_id,
                    (SELECT id FROM users WHERE org_id = :org_id AND role = 'owner' ORDER BY created_at LIMIT 1),
                    'personal')
        """), {
            "content": content,
            "embedding": vec_to_pg(embedding) if embedding else None,
            "tags": [t for o in overlaps for t in o["shared_topics"]],
            "org_id": org_id,
        })
