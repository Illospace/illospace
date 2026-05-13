"""
Knowledge Graph — structured relationship layer over vector memory.

Extends the existing flat vector search with typed edges and graph traversal.
Does NOT replace pgvector — adds a relationship layer on top.

Edge types:
- similar_to: embedding proximity (existing, auto-generated)
- consolidated_from: episodic->semantic lineage (from consolidate.py)
- crystallized_from: semantic->procedural lineage
- contradicts: conflicting information (auto-detected)
- depends_on: skill/memory dependency
- derived_from: memory created from skill execution
- caused_by: causal link between events

Graph-augmented retrieval:
1. Vector search -> top-N candidates
2. Graph traversal -> 1-hop connected memories
3. Merge + re-rank by relevance + relationship strength
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy import text

from brain.platform.db.repositories.memory_visibility import MemoryVisibilityContext, memory_visibility_sql
from brain.systems.memory.truth_maintenance import (
    build_truth_state,
    memory_retrieval_bonus,
    memory_retrieval_priority,
    quarantine_filter_enabled,
)

logger = logging.getLogger("cognition.graph")

# Typed edge relationships
EDGE_TYPES = frozenset({
    "similar_to",         # embedding proximity
    "consolidated_from",  # episodic->semantic
    "crystallized_from",  # semantic->procedural
    "contradicts",        # conflicting info
    "depends_on",         # dependency
    "derived_from",       # execution lineage
    "caused_by",          # causal link
    "related_to",         # manual/generic
})

# Relationship weights for re-ranking
EDGE_WEIGHT_BONUS = {
    "contradicts": 0.15,       # contradictions are very relevant
    "depends_on": 0.12,
    "derived_from": 0.10,
    "caused_by": 0.10,
    "consolidated_from": 0.08,
    "crystallized_from": 0.08,
    "similar_to": 0.05,
    "related_to": 0.03,
}




def graph_augmented_recall(
    session,
    query_emb_str: str,
    limit: int = 5,
    hops: int = 1,
    user_id: str | None = None,
    org_id: str | None = None,
    service_retrieval: bool = False,
) -> list[dict]:
    """Compatibility wrapper around MemoryRepository graph recall."""
    from brain.platform.db.repositories.memories import MemoryRepository

    visibility_context = MemoryVisibilityContext(
        user_id=user_id,
        org_id=org_id,
        allow_global=service_retrieval or (user_id == "system"),
        principal_type="service" if service_retrieval or user_id == "system" else None,
    )
    return MemoryRepository(session).graph_augmented_recall(
        query_embedding=query_emb_str,
        limit=limit,
        hops=hops,
        context=visibility_context,
    )


def auto_link_memory(session, memory_id: int, content: str, memory_type: str) -> dict:
    """Auto-create edges for a newly created memory.

    1. High-similarity edges (already done by add_memory, this adds typed ones)
    2. Contradiction detection
    3. Skill execution lineage

    Returns stats dict.
    """
    stats = {
        "edges_created": 0,
        "contradictions": 0,
        "contradiction_candidates": 0,
        "contradiction_records": 0,
        "contradiction_record_ids": [],
    }

    return stats


def detect_contradictions(session, limit: int = 20) -> list[dict]:
    """Find potential contradictions in the memory graph.

    Automated contradiction candidates now come from explicit review flows.
    """
    del session, limit
    return []




def get_memory_neighborhood(
    session,
    memory_id: int,
    hops: int = 1,
    user_id: str | None = None,
    org_id: str | None = None,
) -> dict:
    """Compatibility wrapper around MemoryRepository neighborhood reads."""
    from brain.platform.db.repositories.memories import MemoryRepository

    visibility_context = MemoryVisibilityContext(
        user_id=user_id,
        org_id=org_id,
        allow_global=(user_id == "system"),
    )
    return MemoryRepository(session).get_memory_neighborhood(
        memory_id,
        hops=hops,
        context=visibility_context,
    )

# -- Internal helpers --------------------------------------------------------
