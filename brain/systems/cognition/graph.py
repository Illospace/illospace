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


def _legacy_graph_augmented_recall(
    session,
    query_emb_str: str,
    limit: int = 5,
    hops: int = 1,
    user_id: str | None = None,
    org_id: str | None = None,
) -> list[dict]:
    """Vector search + graph traversal for richer recall.

    Visibility filtering (multiplayer):
      - If user_id/org_id provided: return private memories owned by user
        OR team/org memories in the same org.
      - If not provided (single-user mode): no filtering -- backward compatible.

    1. Vector search -> top-N candidates (2x limit for pool)
    2. 1-hop graph traversal from candidates (with visibility re-filtering)
    3. Merge, deduplicate, re-rank

    Args:
        session: SQLAlchemy session
        query_emb_str: Embedding string for vector search
        limit: Max results to return
        hops: Graph traversal depth (1 or 2)
        user_id: Current user UUID (for visibility scoping)
        org_id: Current org UUID (for visibility scoping)

    Returns:
        List of memory dicts with graph context.
    """
    # Build visibility filter (multiplayer)
    params: dict = {"emb": query_emb_str}
    visibility_context = MemoryVisibilityContext(
        user_id=user_id,
        org_id=org_id,
        allow_global=(user_id == "system"),
    )
    truth_clause = ""
    if quarantine_filter_enabled():
        truth_clause = """
          AND COALESCE(m.truth_status, 'unknown') NOT IN ('quarantined', 'expired', 'superseded')
          AND COALESCE(m.review_status, 'unreviewed') != 'rejected'
          AND m.demoted_at IS NULL
          AND (m.valid_until IS NULL OR m.valid_until >= NOW())
        """
    vis_clause, vis_params = memory_visibility_sql(visibility_context, alias="m")
    params.update(vis_params)

    # Step 1: Vector search (larger pool for merging)
    pool_size = limit * 3
    params["pool_size"] = pool_size
    vector_results = session.execute(text(f"""
        SELECT m.id, m.content, m.memory_type, m.salience,
               COALESCE(m.memory_tier, 'episodic') as memory_tier,
               COALESCE(m.visibility, 'private') as visibility,
               COALESCE(m.truth_status, 'unknown') as truth_status,
               COALESCE(m.review_status, 'unreviewed') as review_status,
               COALESCE(m.confidence, 0.5) as confidence,
               COALESCE(m.freshness_score, 0.5) as freshness_score,
               m.valid_until,
               m.demoted_at,
               m.policy_kind,
               m.policy_scope,
               m.reviewed_at,
               1 - (m.semantic_embedding <=> CAST(:emb AS vector)) as similarity
        FROM memories m
        WHERE NOT m.archived
          AND m.superseded_by IS NULL
          AND m.semantic_embedding IS NOT NULL
          AND 1 - (m.semantic_embedding <=> CAST(:emb AS vector)) > 0.40
          {truth_clause}
          {vis_clause}
        ORDER BY
            CASE WHEN m.memory_type IN ('lesson', 'pattern') THEN 0.15 ELSE 0 END
            + (1 - (m.semantic_embedding <=> CAST(:emb AS vector))) * 0.60
            + (m.salience / 10.0) * 0.15
            + CASE WHEN COALESCE(m.memory_tier, 'episodic') = 'procedural' THEN 0.10
                   WHEN COALESCE(m.memory_tier, 'episodic') = 'semantic' THEN 0.05
                   ELSE 0.0 END
            DESC
        LIMIT :pool_size
    """), params).mappings().all()

    if not vector_results:
        return []

    # Build result map keyed by memory ID
    seen = {}
    for r in vector_results:
        state = build_truth_state(r)
        if quarantine_filter_enabled() and not state["is_active"]:
            continue
        seen[r["id"]] = {
            "id": r["id"],
            "content": r["content"][:300],
            "type": r["memory_type"],
            "tier": r["memory_tier"],
            "salience": float(r["salience"]) if r["salience"] else 0,
            "similarity": round(float(r["similarity"] or 0), 3),
            "visibility": r.get("visibility", "private"),
            "truth_status": state["truth_status"],
            "review_status": state["review_status"],
            "confidence": round(float(state["confidence"] or 0.5), 3),
            "freshness_score": round(float(state["freshness_score"] or 0.5), 3),
            "policy_kind": state.get("policy_kind"),
            "policy_scope": state.get("policy_scope"),
            "contradiction_status": state["contradiction_status"],
            "is_reviewed_active": state["is_reviewed_active"],
            "is_policy_effective": state["is_policy_effective"],
            "graph_edges": [],
            "priority": memory_retrieval_priority(r),
            "score": float(r["similarity"] or 0) + memory_retrieval_bonus(r),
        }

    if not seen:
        return []

    # Step 2: Graph traversal (1-hop from top candidates)
    # IMPORTANT: re-apply visibility filter on connected memories to prevent
    # private memory leakage through graph edges.
    seed_ids = list(seen.keys())[:limit]
    if seed_ids:
        graph_params: dict = {"seed_ids": seed_ids}
        graph_vis, graph_vis_params = memory_visibility_sql(
            visibility_context,
            alias="m",
            user_param="gvis_user_id",
            org_param="gvis_org_id",
        )
        graph_params.update(graph_vis_params)

        graph_params["graph_limit"] = limit * 2
        graph_results = session.execute(text(f"""
            SELECT
                e.source_id, e.target_id, e.relationship, e.weight,
                m.id as connected_id, m.content, m.memory_type, m.salience,
                COALESCE(m.memory_tier, 'episodic') as memory_tier,
                COALESCE(m.visibility, 'private') as visibility,
                COALESCE(m.truth_status, 'unknown') as truth_status,
                COALESCE(m.review_status, 'unreviewed') as review_status,
                COALESCE(m.confidence, 0.5) as confidence,
                COALESCE(m.freshness_score, 0.5) as freshness_score,
                m.valid_until,
                m.demoted_at,
                m.policy_kind,
                m.policy_scope,
                m.reviewed_at
            FROM edges e
            JOIN memories m ON m.id = CASE
                WHEN e.source_id = ANY(:seed_ids) THEN e.target_id
                ELSE e.source_id
            END
            WHERE (e.source_id = ANY(:seed_ids) OR e.target_id = ANY(:seed_ids))
              AND NOT m.archived
              AND m.id != ALL(:seed_ids)
              {truth_clause}
              {graph_vis}
            ORDER BY e.weight DESC
            LIMIT :graph_limit
        """), graph_params).mappings().all()

        for gr in graph_results:
            cid = gr["connected_id"]
            rel = gr["relationship"]
            bonus = EDGE_WEIGHT_BONUS.get(rel, 0.03)
            state = build_truth_state(gr)
            if quarantine_filter_enabled() and not state["is_active"]:
                continue

            # Track which seed it came from
            from_id = gr["source_id"] if gr["source_id"] in seed_ids else gr["target_id"]
            retrieval_bonus = memory_retrieval_bonus(gr)
            priority = memory_retrieval_priority(gr)

            if cid in seen:
                # Boost existing result
                seen[cid]["score"] += bonus * float(gr["weight"]) + retrieval_bonus
                seen[cid]["priority"] = min(seen[cid]["priority"], priority)
                seen[cid]["truth_status"] = state["truth_status"]
                seen[cid]["review_status"] = state["review_status"]
                seen[cid]["confidence"] = round(float(state["confidence"] or 0.5), 3)
                seen[cid]["freshness_score"] = round(float(state["freshness_score"] or 0.5), 3)
                seen[cid]["policy_kind"] = state.get("policy_kind")
                seen[cid]["policy_scope"] = state.get("policy_scope")
                seen[cid]["contradiction_status"] = state["contradiction_status"]
                seen[cid]["is_reviewed_active"] = state["is_reviewed_active"]
                seen[cid]["is_policy_effective"] = state["is_policy_effective"]
                seen[cid]["graph_edges"].append({
                    "relationship": rel,
                    "from_memory": from_id,
                    "weight": round(float(gr["weight"]), 3),
                })
            else:
                # Add new graph-discovered memory
                seen[cid] = {
                    "id": cid,
                    "content": gr["content"][:300],
                    "type": gr["memory_type"],
                    "tier": gr["memory_tier"],
                    "salience": float(gr["salience"]) if gr["salience"] else 0,
                    "similarity": 0.0,  # not from vector search
                    "truth_status": state["truth_status"],
                    "review_status": state["review_status"],
                    "confidence": round(float(state["confidence"] or 0.5), 3),
                    "freshness_score": round(float(state["freshness_score"] or 0.5), 3),
                    "policy_kind": state.get("policy_kind"),
                    "policy_scope": state.get("policy_scope"),
                    "contradiction_status": state["contradiction_status"],
                    "is_reviewed_active": state["is_reviewed_active"],
                    "is_policy_effective": state["is_policy_effective"],
                    "graph_edges": [{
                        "relationship": rel,
                        "from_memory": from_id,
                        "weight": round(float(gr["weight"]), 3),
                    }],
                    "priority": priority,
                    "score": bonus * float(gr["weight"]) + (float(gr["salience"] or 0) / 10.0) * 0.1 + retrieval_bonus,
                }

    # Step 3: Re-rank and return top results
    ranked = sorted(seen.values(), key=lambda x: (x["priority"], -x["score"]))

    # Update access counts for returned memories
    result_ids = [r["id"] for r in ranked[:limit]]
    if result_ids:
        session.execute(text("""
            UPDATE memories SET last_accessed = NOW(), access_count = access_count + 1
            WHERE id = ANY(:result_ids)
        """), {"result_ids": result_ids})

        # Also boost edge activation counts
        session.execute(text("""
            UPDATE edges SET last_activated = NOW(), activation_count = activation_count + 1
            WHERE (source_id = ANY(:result_ids) OR target_id = ANY(:result_ids))
              AND (source_id = ANY(:result_ids) AND target_id = ANY(:result_ids))
        """), {"result_ids": result_ids})

    results = ranked[:limit]
    for item in results:
        item.pop("priority", None)
    return results


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


def _legacy_get_memory_neighborhood(
    session,
    memory_id: int,
    hops: int = 1,
    user_id: str | None = None,
    org_id: str | None = None,
) -> dict:
    """Get a memory and its graph neighborhood."""
    visibility_context = MemoryVisibilityContext(
        user_id=user_id,
        org_id=org_id,
        allow_global=(user_id == "system"),
    )
    center_vis, center_vis_params = memory_visibility_sql(visibility_context, alias="", user_param="center_user_id", org_param="center_org_id")
    center = session.execute(text("""
        SELECT id, content, memory_type, salience,
               COALESCE(memory_tier, 'episodic') as memory_tier,
               COALESCE(truth_status, 'unknown') as truth_status,
               COALESCE(review_status, 'unreviewed') as review_status,
               COALESCE(confidence, 0.5) as confidence
        FROM memories WHERE id = :mem_id
        {center_vis}
    """.format(center_vis=center_vis)), {"mem_id": memory_id, **center_vis_params}).mappings().first()

    if not center:
        return {"error": f"Memory {memory_id} not found"}

    # Get connected memories
    neighbor_vis, neighbor_vis_params = memory_visibility_sql(
        visibility_context,
        alias="m",
        user_param="neighbor_user_id",
        org_param="neighbor_org_id",
    )
    neighbors = session.execute(text("""
        SELECT
            e.relationship, e.weight,
            m.id, m.content, m.memory_type, m.salience,
            COALESCE(m.memory_tier, 'episodic') as memory_tier,
            COALESCE(m.truth_status, 'unknown') as truth_status,
            COALESCE(m.review_status, 'unreviewed') as review_status,
            CASE WHEN e.source_id = :mem_id THEN 'outgoing' ELSE 'incoming' END as direction
        FROM edges e
        JOIN memories m ON m.id = CASE
            WHEN e.source_id = :mem_id THEN e.target_id ELSE e.source_id
        END
        WHERE (e.source_id = :mem_id OR e.target_id = :mem_id)
          AND NOT m.archived
          {neighbor_vis}
        ORDER BY e.weight DESC
        LIMIT 20
    """.format(neighbor_vis=neighbor_vis)), {"mem_id": memory_id, **neighbor_vis_params}).mappings().all()

    return {
        "center": {
            "id": center["id"],
            "content": center["content"][:300],
            "type": center["memory_type"],
            "tier": center["memory_tier"],
            "truth_status": center.get("truth_status", "unknown"),
            "review_status": center.get("review_status", "unreviewed"),
            "salience": float(center["salience"] or 0),
        },
        "edges": [{
            "relationship": n["relationship"],
            "direction": n["direction"],
            "weight": round(float(n["weight"]), 3),
            "memory": {
                "id": n["id"],
                "content": n["content"][:200],
                "type": n["memory_type"],
                "tier": n["memory_tier"],
                "truth_status": n.get("truth_status", "unknown"),
                "review_status": n.get("review_status", "unreviewed"),
            },
        } for n in neighbors],
        "edge_count": len(neighbors),
    }


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

def _create_edge(
    session, source_id: int, target_id: int,
    relationship: str, weight: float = 1.0,
) -> None:
    """Create or update an edge."""
    session.execute(text("""
        INSERT INTO edges (source_id, target_id, relationship, weight, auto_generated)
        VALUES (:source_id, :target_id, :relationship, :weight, TRUE)
        ON CONFLICT (source_id, target_id, relationship)
        DO UPDATE SET weight = GREATEST(edges.weight, EXCLUDED.weight),
                      last_activated = NOW()
    """), {"source_id": source_id, "target_id": target_id,
           "relationship": relationship, "weight": weight})
