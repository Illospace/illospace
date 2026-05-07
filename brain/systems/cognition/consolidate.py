"""
Hierarchical Memory Consolidation — episodic → semantic → procedural.

Biological brains consolidate: raw episodes become semantic knowledge
through repetition, and practiced procedures become muscle memory.

Three consolidation operations:
1. cluster_episodes() — Group similar episodic memories
2. extract_semantic() — Synthesize clusters into semantic knowledge
3. crystallize_procedural() — Crystallize repeated "how to" patterns into skill procedures

Plus forgetting curve:
4. apply_forgetting_curve() — Decay salience of unrecalled memories
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
from sqlalchemy import text

from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.memory.truth_maintenance import (
    build_consolidation_truth_fields,
    record_memory_review,
)

logger = logging.getLogger("cognition.consolidate")

# Minimum episodes to form a cluster
MIN_CLUSTER_SIZE = 3
# Similarity threshold for clustering
CLUSTER_SIMILARITY = 0.65
# Days before forgetting curve starts
FORGETTING_GRACE_DAYS = 7
# Salience decay rate per day (multiplicative)
DECAY_RATE = 0.97
# Minimum salience before archival
ARCHIVE_THRESHOLD = 1.0
# High-salience memories immune to decay
IMMUNE_SALIENCE = 8.0
SYSTEM_USER_ID = "00000000-0000-0000-0000-000000000000"
COMPACTION_VISIBILITIES = ("private", "team", "org", "system")


@dataclass(frozen=True, slots=True)
class ConsolidationScope:
    """Tenant/visibility boundary for any consolidation or compaction pass."""

    visibility: str = "private"
    org_id: str | None = None
    user_id: str | None = None

    def __post_init__(self) -> None:
        visibility = _normalize_visibility(self.visibility)
        object.__setattr__(self, "visibility", visibility)
        object.__setattr__(self, "org_id", _text_or_none(self.org_id))
        object.__setattr__(self, "user_id", _text_or_none(self.user_id))

    @property
    def key(self) -> str:
        return (
            f"visibility={self.visibility};"
            f"org={self.org_id or 'none'};"
            f"user={self.user_id or 'shared'}"
        )


def _normalize_visibility(value: str | None) -> str:
    visibility = str(value or "private").strip().lower()
    if visibility not in COMPACTION_VISIBILITIES:
        return "private"
    return visibility


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text_value = str(value).strip()
    return text_value or None


def _scope_from_args(
    *,
    scope: ConsolidationScope | None = None,
    org_id: str | None = None,
    user_id: str | None = None,
    visibility: str | None = None,
) -> ConsolidationScope:
    if scope is not None:
        return scope
    return ConsolidationScope(
        visibility=visibility or "private",
        org_id=org_id,
        user_id=user_id,
    )


def _require_consolidation_scope(
    *,
    scope: ConsolidationScope | None = None,
    org_id: str | None = None,
    user_id: str | None = None,
    visibility: str | None = None,
) -> ConsolidationScope:
    scoped = _scope_from_args(
        scope=scope,
        org_id=org_id,
        user_id=user_id,
        visibility=visibility,
    )
    if scoped.visibility == "private" and not scoped.user_id:
        raise ValueError("private consolidation requires user_id")
    if scoped.visibility in {"team", "org"} and not scoped.org_id:
        raise ValueError(f"{scoped.visibility} consolidation requires org_id")
    return scoped


def _scope_sql(scope: ConsolidationScope, *, alias: str = "m") -> tuple[str, dict[str, str | None]]:
    prefix = f"{alias}." if alias else ""
    clause = f"""
      AND COALESCE({prefix}visibility, 'private') = :scope_visibility
    """
    params: dict[str, str | None] = {
        "scope_visibility": scope.visibility,
    }
    if scope.org_id is not None:
        clause += f"\n      AND {prefix}org_id = :scope_org_id"
        params["scope_org_id"] = scope.org_id
    if scope.user_id is not None:
        clause += f"\n      AND {prefix}user_id = :scope_user_id"
        params["scope_user_id"] = scope.user_id
    return clause, params


def _memory_matches_scope(memory: Any, scope: ConsolidationScope) -> bool:
    visibility = _normalize_visibility(getattr(memory, "visibility", None))
    if visibility != scope.visibility:
        return False
    if scope.org_id is not None and _text_or_none(getattr(memory, "org_id", None)) != scope.org_id:
        return False
    if scope.user_id is not None and _text_or_none(getattr(memory, "user_id", None)) != scope.user_id:
        return False
    return True


def _mapping_matches_scope(row: Mapping[str, Any], scope: ConsolidationScope) -> bool:
    visibility = _normalize_visibility(row.get("visibility"))
    if visibility != scope.visibility:
        return False
    if scope.org_id is not None and _text_or_none(row.get("org_id")) != scope.org_id:
        return False
    if scope.user_id is not None and _text_or_none(row.get("user_id")) != scope.user_id:
        return False
    return True


def _summary_owner_id(scope: ConsolidationScope, children: Sequence[Any]) -> str:
    if scope.user_id:
        return scope.user_id
    for child in children:
        if isinstance(child, Mapping):
            child_user_id = _text_or_none(child.get("user_id"))
        else:
            child_user_id = _text_or_none(getattr(child, "user_id", None))
        if child_user_id:
            return child_user_id
    return SYSTEM_USER_ID


def _discover_consolidation_scopes(
    *,
    org_id: str | None = None,
    limit: int = 500,
) -> list[ConsolidationScope]:
    """Discover concrete memory scopes so consolidation never runs globally."""
    with UnitOfWork() as uow:
        result = uow.session.execute(text("""
            SELECT DISTINCT
                   COALESCE(visibility, 'private') AS visibility,
                   org_id,
                   CASE
                       WHEN COALESCE(visibility, 'private') = 'private' THEN user_id
                       ELSE NULL
                   END AS user_id
            FROM memories
            WHERE NOT archived
              AND COALESCE(visibility, 'private') IN ('private', 'team', 'org', 'system')
              AND (:org_id IS NULL OR org_id = :org_id)
              AND (
                    COALESCE(visibility, 'private') <> 'private'
                    OR user_id IS NOT NULL
                  )
            ORDER BY visibility, org_id, user_id
            LIMIT :limit
        """), {"org_id": org_id, "limit": limit})
        rows = result.mappings().all()

    scopes: list[ConsolidationScope] = []
    seen: set[str] = set()
    for row in rows:
        try:
            scope = _require_consolidation_scope(
                org_id=_text_or_none(row.get("org_id")),
                user_id=_text_or_none(row.get("user_id")),
                visibility=_normalize_visibility(row.get("visibility")),
            )
        except ValueError:
            logger.debug("Skipping incomplete consolidation scope row: %s", dict(row))
            continue
        if scope.key in seen:
            continue
        seen.add(scope.key)
        scopes.append(scope)
    return scopes


def _single_or_discovered_scopes(
    *,
    scope: ConsolidationScope | None = None,
    org_id: str | None = None,
    user_id: str | None = None,
    visibility: str | None = None,
) -> list[ConsolidationScope]:
    """Resolve explicit args to one scope, otherwise discover all active scopes."""
    if scope is not None:
        return [_require_consolidation_scope(scope=scope)]
    if org_id or user_id or visibility:
        return [_require_consolidation_scope(
            org_id=org_id,
            user_id=user_id,
            visibility=visibility,
        )]
    return _discover_consolidation_scopes()


def _sum_counts(target: dict[str, int], source: Mapping[str, Any], keys: Iterable[str]) -> None:
    for key in keys:
        target[key] = int(target.get(key, 0)) + int(source.get(key, 0) or 0)


def cluster_episodes(
    limit: int = 200,
    *,
    scope: ConsolidationScope | None = None,
    org_id: str | None = None,
    user_id: str | None = None,
    visibility: str | None = None,
) -> list[list[int]]:
    """Find clusters of similar unconsolidated episodic memories.

    Uses greedy nearest-neighbor clustering: pick a seed, collect
    neighbors above threshold, remove from pool, repeat.

    Returns list of clusters (each cluster is a list of memory IDs).
    """
    scoped = _require_consolidation_scope(
        scope=scope,
        org_id=org_id,
        user_id=user_id,
        visibility=visibility,
    )
    scope_clause, scope_params = _scope_sql(scoped, alias="m")

    with UnitOfWork() as uow:
        # Fetch unconsolidated episodic memories
        result = uow.session.execute(text(f"""
            SELECT id, content, semantic_embedding, user_id, org_id, visibility
            FROM memories m
            WHERE NOT archived AND NOT consolidated
              AND memory_tier = 'episodic'
              AND memory_type IN ('episode', 'lesson', 'insight', 'pattern')
              AND semantic_embedding IS NOT NULL
              AND COALESCE(truth_status, 'unknown') NOT IN ('quarantined', 'expired', 'superseded')
              AND demoted_at IS NULL
              {scope_clause}
            ORDER BY created_at DESC
            LIMIT :limit
        """), {"limit": limit, **scope_params})
        rows = [
            row for row in result.mappings().all()
            if _mapping_matches_scope(row, scoped)
        ]

    if len(rows) < MIN_CLUSTER_SIZE:
        return []

    ids = [r["id"] for r in rows]
    # Parse embeddings
    embeddings = []
    for r in rows:
        emb = r["semantic_embedding"]
        if isinstance(emb, str):
            emb = [float(x) for x in emb.strip("[]").split(",")]
        embeddings.append(emb)

    embeddings = np.array(embeddings)
    # Normalize for cosine similarity
    norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1, norms)
    normed = embeddings / norms

    # Compute similarity matrix
    sim_matrix = normed @ normed.T

    # Greedy clustering
    used = set()
    clusters = []

    for i in range(len(ids)):
        if i in used:
            continue
        # Find neighbors
        neighbors = [j for j in range(len(ids))
                     if j != i and j not in used
                     and sim_matrix[i, j] >= CLUSTER_SIMILARITY]

        if len(neighbors) >= MIN_CLUSTER_SIZE - 1:
            cluster = [ids[i]] + [ids[j] for j in neighbors]
            clusters.append(cluster)
            used.add(i)
            used.update(neighbors)

    logger.info(
        "Found %d episode clusters from %d memories in %s",
        len(clusters),
        len(ids),
        scoped.key,
    )
    return clusters


def extract_semantic(
    cluster_ids: list[int],
    user_id: str | None = None,
    *,
    scope: ConsolidationScope | None = None,
    org_id: str | None = None,
    visibility: str | None = None,
) -> int | None:
    """Synthesize a cluster of episodes into one semantic memory.

    Uses Ollama to distill common knowledge from the episodes.
    Falls back to heuristic extraction if Ollama unavailable.

    Returns the new semantic memory ID, or None on failure.
    """
    scoped = _require_consolidation_scope(
        scope=scope,
        org_id=org_id,
        user_id=user_id,
        visibility=visibility,
    )
    scope_clause, scope_params = _scope_sql(scoped, alias="m")

    with UnitOfWork() as uow:
        # Fetch episode contents
        result = uow.session.execute(text(f"""
            SELECT id, content, memory_type, salience, tags, user_id, org_id, visibility
            FROM memories m
            WHERE id = ANY(:cluster_ids)
              AND COALESCE(truth_status, 'unknown') NOT IN ('quarantined', 'expired', 'superseded')
              AND demoted_at IS NULL
              {scope_clause}
            ORDER BY salience DESC, created_at DESC
        """), {"cluster_ids": cluster_ids, **scope_params})
        episodes = [
            row for row in result.mappings().all()
            if _mapping_matches_scope(row, scoped)
        ]

        if not episodes or len(episodes) != len(set(cluster_ids)):
            logger.warning(
                "Skipping semantic extraction for cluster outside %s: requested=%s found=%s",
                scoped.key,
                len(set(cluster_ids)),
                len(episodes),
            )
            return None

        # Build context for synthesis
        episode_texts = []
        all_tags = set()
        max_salience = 0.0
        for ep in episodes:
            episode_texts.append(f"- [{ep['memory_type']}] {ep['content'][:300]}")
            if ep["tags"]:
                all_tags.update(ep["tags"])
            max_salience = max(max_salience, ep["salience"] or 0)

        episodes_context = "\n".join(episode_texts)

        # Try Ollama synthesis
        synthesized = _synthesize_with_gpu_server(episodes_context)
        if not synthesized:
            # Heuristic fallback: take highest-salience episode + count
            best = episodes[0]
            synthesized = (
                f"[consolidated from {len(episodes)} episodes] "
                f"{best['content'][:400]}"
            )

        # Compute embedding for the new semantic memory
        try:
            from brain.systems.memory.embeddings import embed_document, make_emotional_embedding, vec_to_pg
            semantic_emb = embed_document(synthesized)
            source_ref = f"cluster:{scoped.visibility}:{len(cluster_ids)}:{cluster_ids[0]}-{cluster_ids[-1]}"
            promotion_evidence = {
                "source_memory_ids": cluster_ids,
                "support_count": len(cluster_ids),
                "source_ref": source_ref,
                "scope": {
                    "org_id": scoped.org_id,
                    "user_id": scoped.user_id,
                    "visibility": scoped.visibility,
                },
            }
            confidence = min(0.75, max(0.5, max_salience / 10.0))
            truth_fields = build_consolidation_truth_fields(
                source_kind="consolidation",
                source_ref=source_ref,
                confidence=confidence,
                evidence=promotion_evidence,
                target_tier="semantic",
                support_count=len(cluster_ids),
                reviewed_by=user_id,
            )

            # Create semantic memory
            ins_result = uow.session.execute(text("""
                INSERT INTO memories (
                    content, memory_type, memory_tier, semantic_embedding,
                    emotional_embedding, salience, source, tags,
                    user_id, org_id, visibility,
                    source_memory_ids, decay_eligible, truth_status,
                    review_status, confidence, freshness_score,
                    source_type, source_ref, valid_from, reviewed_at, reviewed_by
                ) VALUES (
                    :content, 'pattern', 'semantic', CAST(:semantic_emb AS vector),
                    CAST(:emotional_emb AS vector), :salience, 'consolidation', :tags,
                    :owner_user_id, :owner_org_id, :visibility,
                    :source_ids, FALSE, :truth_status, :review_status, :confidence,
                    :freshness_score, :source_type, :source_ref, :valid_from, :reviewed_at,
                    :reviewed_by
                ) RETURNING id
            """), {
                "content": synthesized,
                "semantic_emb": vec_to_pg(semantic_emb),
                "emotional_emb": vec_to_pg(make_emotional_embedding(0, 0, "neutral")),
                "salience": min(10.0, max_salience + 1.0),
                "tags": list(all_tags)[:20],
                "owner_user_id": _summary_owner_id(scoped, episodes),
                "owner_org_id": scoped.org_id,
                "visibility": scoped.visibility,
                "source_ids": cluster_ids,
                **truth_fields,
            })
            new_id = ins_result.mappings().first()["id"]

            record_memory_review(
                uow.session,
                memory_id=new_id,
                action="promote",
                from_tier="episodic",
                to_tier="semantic",
                reviewer_id=user_id,
                rationale=f"Consolidated {len(cluster_ids)} episodic memories into a semantic memory.",
                evidence=promotion_evidence,
                confidence=confidence,
            )

            # Mark source episodes as consolidated
            uow.session.execute(text(f"""
                UPDATE memories m SET consolidated = TRUE
                WHERE id = ANY(:cluster_ids)
                  {scope_clause}
            """), {"cluster_ids": cluster_ids, **scope_params})

            # Create lineage edges
            for eid in cluster_ids:
                uow.session.execute(text("""
                    INSERT INTO edges (source_id, target_id, relationship, weight, auto_generated)
                    VALUES (:source_id, :target_id, 'consolidated_from', 1.0, TRUE)
                    ON CONFLICT (source_id, target_id, relationship) DO NOTHING
                """), {"source_id": new_id, "target_id": eid})

            logger.info(f"Created semantic memory {new_id} from {len(cluster_ids)} episodes")
            return new_id

        except Exception as e:
            logger.error(f"Failed to create semantic memory: {e}")
            return None


def crystallize_procedural(
    skill_name: str,
    user_id: str | None = None,
    *,
    scope: ConsolidationScope | None = None,
    org_id: str | None = None,
    visibility: str | None = None,
) -> int | None:
    """Crystallize repeated semantic "how to" patterns into a skill procedure.

    Looks for semantic memories tagged with a skill that describe procedures,
    and merges them into a single procedural memory that updates the skill.

    Returns the new procedural memory ID, or None if not enough data.
    """
    scoped = _require_consolidation_scope(
        scope=scope,
        org_id=org_id,
        user_id=user_id,
        visibility=visibility,
    )
    scope_clause, scope_params = _scope_sql(scoped, alias="m")

    with UnitOfWork() as uow:
        # Find semantic memories related to this skill
        result = uow.session.execute(text(f"""
            SELECT id, content, salience, user_id, org_id, visibility
            FROM memories m
            WHERE NOT archived AND memory_tier = 'semantic'
              AND COALESCE(truth_status, 'unknown') NOT IN ('quarantined', 'expired', 'superseded')
              AND demoted_at IS NULL
              AND (tags @> ARRAY[:skill_name] OR content ILIKE :skill_pattern)
              AND memory_type IN ('pattern', 'lesson')
              {scope_clause}
            ORDER BY salience DESC
            LIMIT 10
        """), {
            "skill_name": skill_name,
            "skill_pattern": f"%{skill_name}%",
            **scope_params,
        })
        semantics = [
            row for row in result.mappings().all()
            if _mapping_matches_scope(row, scoped)
        ]

        if len(semantics) < 2:
            return None

        # Check if we already have a procedural memory for this skill
        existing_result = uow.session.execute(text(f"""
            SELECT id FROM memories m
            WHERE NOT archived AND memory_tier = 'procedural'
              AND COALESCE(truth_status, 'unknown') NOT IN ('quarantined', 'expired', 'superseded')
              AND demoted_at IS NULL
              AND tags @> ARRAY[:skill_name]
              {scope_clause}
            LIMIT 1
        """), {"skill_name": skill_name, **scope_params})
        existing = existing_result.mappings().first()

        # Build procedure from semantic memories
        contents = [s["content"][:300] for s in semantics]
        procedure_text = _crystallize_with_gpu_server(skill_name, contents)
        if not procedure_text:
            procedure_text = (
                f"[procedural: {skill_name}] "
                + " | ".join(c[:150] for c in contents[:5])
            )

        try:
            from brain.systems.memory.embeddings import embed_document, make_emotional_embedding, vec_to_pg
            semantic_emb = embed_document(procedure_text)
            source_ids = [s["id"] for s in semantics]
            max_sal = max(s["salience"] or 5 for s in semantics)
            source_ref = f"skill:{scoped.visibility}:{skill_name}"[:120]
            promotion_evidence = {
                "skill_name": skill_name,
                "source_memory_ids": source_ids,
                "support_count": len(source_ids),
                "source_ref": source_ref,
                "scope": {
                    "org_id": scoped.org_id,
                    "user_id": scoped.user_id,
                    "visibility": scoped.visibility,
                },
            }
            confidence = min(0.8, max(0.55, max_sal / 10.0))
            truth_fields = build_consolidation_truth_fields(
                source_kind="consolidation",
                source_ref=source_ref,
                confidence=confidence,
                evidence=promotion_evidence,
                target_tier="procedural",
                support_count=len(source_ids),
                reviewed_by=user_id,
            )

            if existing:
                # Update existing procedural memory
                uow.session.execute(text("""
                    UPDATE memories SET
                        content = :content,
                        semantic_embedding = CAST(:semantic_emb AS vector),
                        salience = :salience,
                        user_id = :owner_user_id,
                        org_id = :owner_org_id,
                        visibility = :visibility,
                        source_memory_ids = :source_ids,
                        last_accessed = NOW(),
                        truth_status = :truth_status,
                        review_status = :review_status,
                        confidence = :confidence,
                        freshness_score = :freshness_score,
                        source_type = :source_type,
                        source_ref = :source_ref,
                        valid_from = :valid_from,
                        reviewed_at = :reviewed_at,
                        reviewed_by = :reviewed_by
                    WHERE id = :mem_id
                    RETURNING id
                """), {
                    "content": procedure_text,
                    "semantic_emb": vec_to_pg(semantic_emb),
                    "salience": min(10.0, max_sal + 1.0),
                    "source_ids": source_ids,
                    "mem_id": existing["id"],
                    "owner_user_id": _summary_owner_id(scoped, semantics),
                    "owner_org_id": scoped.org_id,
                    "visibility": scoped.visibility,
                    **truth_fields,
                })
                proc_id = existing["id"]
                logger.info(f"Updated procedural memory {proc_id} for skill '{skill_name}'")
            else:
                # Create new procedural memory
                ins_result = uow.session.execute(text("""
                    INSERT INTO memories (
                        content, memory_type, memory_tier, semantic_embedding,
                        emotional_embedding, salience, source, tags,
                        user_id, org_id, visibility,
                        source_memory_ids, decay_eligible, truth_status,
                        review_status, confidence, freshness_score,
                        source_type, source_ref, valid_from, reviewed_at, reviewed_by
                    ) VALUES (
                        :content, 'pattern', 'procedural', CAST(:semantic_emb AS vector),
                        CAST(:emotional_emb AS vector), :salience, 'crystallization', :tags,
                        :owner_user_id, :owner_org_id, :visibility,
                        :source_ids, FALSE, :truth_status, :review_status, :confidence,
                        :freshness_score, :source_type, :source_ref, :valid_from, :reviewed_at,
                        :reviewed_by
                    ) RETURNING id
                """), {
                    "content": procedure_text,
                    "semantic_emb": vec_to_pg(semantic_emb),
                    "emotional_emb": vec_to_pg(make_emotional_embedding(0, 0, "neutral")),
                    "salience": min(10.0, max_sal + 1.5),
                    "tags": [skill_name],
                    "owner_user_id": _summary_owner_id(scoped, semantics),
                    "owner_org_id": scoped.org_id,
                    "visibility": scoped.visibility,
                    "source_ids": source_ids,
                    **truth_fields,
                })
                proc_id = ins_result.mappings().first()["id"]
                logger.info(f"Created procedural memory {proc_id} for skill '{skill_name}'")

            record_memory_review(
                uow.session,
                memory_id=proc_id,
                action="promote",
                from_tier="semantic",
                to_tier="procedural",
                reviewer_id=user_id,
                rationale=f"Crystallized {len(source_ids)} semantic memories into a procedural memory for {skill_name}.",
                evidence=promotion_evidence,
                confidence=confidence,
            )

            # Create lineage edges
            for sid in source_ids:
                uow.session.execute(text("""
                    INSERT INTO edges (source_id, target_id, relationship, weight, auto_generated)
                    VALUES (:source_id, :target_id, 'crystallized_from', 1.0, TRUE)
                    ON CONFLICT (source_id, target_id, relationship) DO NOTHING
                """), {"source_id": proc_id, "target_id": sid})

            return proc_id

        except Exception as e:
            logger.error(f"Failed to crystallize procedural memory: {e}")
            return None


def apply_forgetting_curve(
    *,
    scope: ConsolidationScope | None = None,
    org_id: str | None = None,
    user_id: str | None = None,
    visibility: str | None = None,
) -> dict:
    """Apply forgetting curve: decay salience of unrecalled memories.

    Rules:
    - Memories not accessed in FORGETTING_GRACE_DAYS start decaying
    - Decay rate: salience *= DECAY_RATE per day since last access
    - High-salience memories (>= IMMUNE_SALIENCE) are immune
    - Memories below ARCHIVE_THRESHOLD get archived
    - Semantic and procedural memories decay slower (0.5x rate)
    """
    cutoff = datetime.now() - timedelta(days=FORGETTING_GRACE_DAYS)
    scoped = (
        _scope_from_args(scope=scope, org_id=org_id, user_id=user_id, visibility=visibility)
        if scope or org_id or user_id or visibility
        else None
    )
    scope_clause = ""
    scope_params: dict[str, str | None] = {}
    if scoped is not None:
        scope_clause, scope_params = _scope_sql(scoped, alias="m")

    with UnitOfWork() as uow:
        # Decay episodic memories
        result_ep = uow.session.execute(text(f"""
            UPDATE memories m SET
                salience = GREATEST(:archive_threshold, salience * POWER(:decay_rate,
                    EXTRACT(EPOCH FROM (NOW() - last_accessed)) / 86400.0 - :grace_days
                ))
            WHERE NOT archived AND decay_eligible
              AND salience < :immune_salience
              AND last_accessed < :cutoff
              AND memory_tier = 'episodic'
              {scope_clause}
            RETURNING id, salience
        """), {
            "archive_threshold": ARCHIVE_THRESHOLD,
            "decay_rate": DECAY_RATE,
            "grace_days": FORGETTING_GRACE_DAYS,
            "immune_salience": IMMUNE_SALIENCE,
            "cutoff": cutoff,
            **scope_params,
        })
        episodic_decayed = result_ep.rowcount

        # Decay semantic memories (slower: sqrt of decay rate)
        semantic_decay = DECAY_RATE ** 0.5  # slower decay
        result_sem = uow.session.execute(text(f"""
            UPDATE memories m SET
                salience = GREATEST(:archive_threshold, salience * POWER(:decay_rate,
                    EXTRACT(EPOCH FROM (NOW() - last_accessed)) / 86400.0 - :grace_days
                ))
            WHERE NOT archived AND decay_eligible
              AND salience < :immune_salience
              AND last_accessed < :cutoff
              AND memory_tier = 'semantic'
              {scope_clause}
            RETURNING id, salience
        """), {
            "archive_threshold": ARCHIVE_THRESHOLD,
            "decay_rate": semantic_decay,
            "grace_days": FORGETTING_GRACE_DAYS * 2,
            "immune_salience": IMMUNE_SALIENCE,
            "cutoff": cutoff,
            **scope_params,
        })
        semantic_decayed = result_sem.rowcount

        # Archive memories below threshold
        result_arch = uow.session.execute(text(f"""
            UPDATE memories m SET archived = TRUE
            WHERE NOT archived AND decay_eligible
              AND salience <= :archive_threshold
              AND memory_tier = 'episodic'
              AND consolidated = TRUE
              {scope_clause}
            RETURNING id
        """), {"archive_threshold": ARCHIVE_THRESHOLD, **scope_params})
        archived = result_arch.rowcount

    stats = {
        "episodic_decayed": episodic_decayed,
        "semantic_decayed": semantic_decayed,
        "archived": archived,
    }
    logger.info(f"Forgetting curve: {stats}")
    return stats


def run_dag_compaction(
    *,
    org_id: str | None = None,
    user_id: str | None = None,
    visibility: str | None = None,
    scope: ConsolidationScope | None = None,
) -> dict:
    """Run DAG-based memory compaction: leaf compression + cascade.

    Phase 1 — Leaf compaction (depth 0):
        Groups unconsolidated episodic memories (not yet in any summary)
        into chunks of 5-8, compresses each into a MemorySummary at depth 0.

    Phase 2 — Cascade (depth 1+):
        For each depth 0..4, if >= 4 summaries exist, compress them to depth+1.
        Max depth is 5 — at depth 4 we merge into existing depth-5 summary.

    Returns stats: {"leaf_passes": N, "cascade_passes": N, "summaries_created": N}
    """
    if scope is not None:
        scopes = [_require_consolidation_scope(scope=scope)]
    elif visibility is not None or user_id is not None:
        scopes = [_require_consolidation_scope(
            org_id=org_id,
            user_id=user_id,
            visibility=visibility,
        )]
    else:
        scopes = _discover_consolidation_scopes(org_id=org_id)

    stats = {
        "leaf_passes": 0,
        "cascade_passes": 0,
        "summaries_created": 0,
        "scopes_processed": 0,
    }
    for scoped in scopes:
        scope_stats = _run_dag_compaction_for_scope(scoped)
        stats["leaf_passes"] += scope_stats.get("leaf_passes", 0)
        stats["cascade_passes"] += scope_stats.get("cascade_passes", 0)
        stats["summaries_created"] += scope_stats.get("summaries_created", 0)
        stats["scopes_processed"] += 1

    logger.info("DAG compaction complete across %d scopes: %s", len(scopes), stats)
    return stats


def _run_dag_compaction_for_scope(
    scope: ConsolidationScope,
) -> dict:
    """Run one DAG compaction pass inside a single tenant/visibility scope."""
    from brain.systems.cognition.dag_compaction import compress_memories
    from brain.platform.db.models.memory_dag import MemorySummary as MemorySummaryModel
    from brain.systems.memory.integrity import run_all_checks
    from brain.platform.providers.model_policy import get_model_for_tier

    stats = {"leaf_passes": 0, "cascade_passes": 0, "summaries_created": 0}

    owner_user_id = scope.user_id or SYSTEM_USER_ID
    low_model = get_model_for_tier(
        "low",
        include_provider_prefix=True,
        user_id=owner_user_id,
        org_id=scope.org_id,
    )
    leaf_model = low_model
    cascade_model = low_model

    try:
        with UnitOfWork() as uow:
            # ── Phase 1: Leaf compaction ──────────────────────────
            # Find episodic memories not yet in any summary (no SummaryLineage row)
            from sqlalchemy import select
            from brain.platform.db.models.memory import Memory
            from brain.platform.db.models.memory_dag import SummaryLineage

            # Subquery: memory IDs already in a summary
            already_summarized = (
                select(SummaryLineage.child_memory_id)
                .where(SummaryLineage.child_memory_id.isnot(None))
                .scalar_subquery()
            )

            # Unconsolidated episodic memories not in any summary
            leaf_stmt = (
                select(Memory)
                .where(Memory.archived != True)  # noqa: E712
                .where(Memory.memory_type.in_(["episode", "lesson", "insight", "pattern"]))
                .where(Memory.id.notin_(already_summarized))
                .where(Memory.visibility == scope.visibility)
                .where(Memory.truth_status.notin_(["quarantined", "expired", "superseded"]))
                .where(Memory.demoted_at.is_(None))
                .order_by(Memory.created_at.desc())
                .limit(200)
            )
            if scope.org_id is not None:
                leaf_stmt = leaf_stmt.where(Memory.org_id == scope.org_id)
            if scope.user_id is not None:
                leaf_stmt = leaf_stmt.where(Memory.user_id == scope.user_id)

            leaf_memories = [
                memory for memory in uow.session.scalars(leaf_stmt).all()
                if _memory_matches_scope(memory, scope)
            ]

            # Group into chunks of 5-8 (target 6)
            CHUNK_SIZE = 6
            chunks = []
            for i in range(0, len(leaf_memories), CHUNK_SIZE):
                chunk = leaf_memories[i:i + CHUNK_SIZE]
                if len(chunk) >= 3:  # minimum viable chunk
                    chunks.append(chunk)

            for chunk in chunks:
                try:
                    contents = [m.content for m in chunk]
                    source_ids = [m.id for m in chunk]

                    result = compress_memories(
                        contents=contents,
                        source_ids=source_ids,
                        depth=0,
                        model=leaf_model,
                        user_id=owner_user_id,
                    )

                    # Create MemorySummary at depth 0
                    # Estimate token count: ~1 token per 4 chars
                    token_count = len(result["content"]) // 4

                    summary = MemorySummaryModel(
                        depth=0,
                        content=result["content"],
                        breadcrumbs=result["breadcrumbs"],
                        token_count=token_count,
                        earliest_at=min((m.created_at for m in chunk if m.created_at), default=None),
                        latest_at=max((m.created_at for m in chunk if m.created_at), default=None),
                        descendant_count=len(chunk),
                        created_by_model=result["model_used"],
                        user_id=owner_user_id,
                        org_id=scope.org_id,
                        visibility=scope.visibility,
                    )
                    uow.session.add(summary)
                    uow.session.flush()

                    # Create SummaryLineage entries
                    for mem in chunk:
                        uow.memory_summaries.add_child_memory(summary.id, mem.id)

                    stats["leaf_passes"] += 1
                    stats["summaries_created"] += 1
                except Exception as e:
                    logger.warning("DAG leaf compaction failed for chunk: %s", e)

            # ── Phase 2: Cascade (depth 1+) ───────────────────────
            MAX_DEPTH = 5
            for depth in range(0, MAX_DEPTH):
                try:
                    summaries_at_depth = uow.memory_summaries.list_by_depth_min_count(
                        depth=depth,
                        min_count=4,
                        org_id=scope.org_id,
                        user_id=scope.user_id,
                        visibility=scope.visibility,
                    )
                    if not summaries_at_depth:
                        continue

                    contents = [s.content for s in summaries_at_depth]
                    source_ids = [s.id for s in summaries_at_depth]
                    new_depth = min(depth + 1, MAX_DEPTH)

                    result = compress_memories(
                        contents=contents,
                        source_ids=source_ids,
                        depth=new_depth,
                        model=cascade_model,
                        user_id=owner_user_id,
                    )

                    token_count = len(result["content"]) // 4
                    total_descendants = sum(s.descendant_count for s in summaries_at_depth)

                    cascade_summary = MemorySummaryModel(
                        depth=new_depth,
                        content=result["content"],
                        breadcrumbs=result["breadcrumbs"],
                        token_count=token_count,
                        earliest_at=min(
                            (s.earliest_at for s in summaries_at_depth if s.earliest_at),
                            default=None,
                        ),
                        latest_at=max(
                            (s.latest_at for s in summaries_at_depth if s.latest_at),
                            default=None,
                        ),
                        descendant_count=total_descendants,
                        created_by_model=result["model_used"],
                        user_id=owner_user_id,
                        org_id=scope.org_id,
                        visibility=scope.visibility,
                    )
                    uow.session.add(cascade_summary)
                    uow.session.flush()

                    # Link child summaries
                    for child_summary in summaries_at_depth:
                        uow.memory_summaries.add_child_summary(
                            cascade_summary.id, child_summary.id
                        )

                    stats["cascade_passes"] += 1
                    stats["summaries_created"] += 1
                except Exception as e:
                    logger.warning("DAG cascade failed at depth %d: %s", depth, e)

    except Exception as e:
        logger.error("DAG compaction transaction failed: %s", e)
        raise

    # Post-compaction integrity checks (non-fatal)
    try:
        run_all_checks(org_id=scope.org_id)
    except Exception as e:
        logger.debug("Post-compaction integrity checks failed: %s", e)

    logger.info("DAG compaction complete for %s: %s", scope.key, stats)
    return stats


def run_consolidation(
    *,
    org_id: str | None = None,
    user_id: str | None = None,
    visibility: str | None = None,
    scope: ConsolidationScope | None = None,
) -> dict:
    """Run the full consolidation pipeline.

    1. Cluster similar episodes
    2. Extract semantic memories from clusters
    3. Crystallize procedural memories for active skills
    4. Apply forgetting curve

    Returns stats dict.
    """
    stats = {
        "clusters_found": 0,
        "semantic_created": 0,
        "procedural_created": 0,
        "forgetting": {},
        "scopes_processed": 0,
    }

    scopes = _single_or_discovered_scopes(
        scope=scope,
        org_id=org_id,
        user_id=user_id,
        visibility=visibility,
    )
    forgetting_totals = {
        "episodic_decayed": 0,
        "semantic_decayed": 0,
        "archived": 0,
    }
    dag_totals = {
        "leaf_passes": 0,
        "cascade_passes": 0,
        "summaries_created": 0,
        "scopes_processed": 0,
    }

    for scoped in scopes:
        stats["scopes_processed"] += 1

        # Step 1+2: Cluster and extract inside one concrete scope.
        clusters = cluster_episodes(scope=scoped)
        stats["clusters_found"] += len(clusters)

        for cluster in clusters:
            new_id = extract_semantic(cluster, user_id=scoped.user_id, scope=scoped)
            if new_id:
                stats["semantic_created"] += 1

        # Step 3: Crystallize for active skills inside the same scope.
        with UnitOfWork() as uow:
            result = uow.session.execute(text("""
                SELECT DISTINCT name FROM skills
                WHERE NOT archived AND use_count >= 3
            """))
            skills = [r["name"] for r in result.mappings().all()]

        for skill_name in skills:
            proc_id = crystallize_procedural(skill_name, user_id=scoped.user_id, scope=scoped)
            if proc_id:
                stats["procedural_created"] += 1

        # Step 4: Forgetting curve
        _sum_counts(forgetting_totals, apply_forgetting_curve(scope=scoped), forgetting_totals.keys())

        # Step 5: DAG compaction (memory-DAG integration)
        try:
            dag_stats = run_dag_compaction(scope=scoped)
            _sum_counts(dag_totals, dag_stats, dag_totals.keys())
        except Exception as e:
            logger.error("DAG compaction failed for %s (non-fatal): %s", scoped.key, e)
            dag_totals.setdefault("errors", 0)
            dag_totals["errors"] += 1

    stats["forgetting"] = forgetting_totals
    stats["dag_compaction"] = dag_totals

    logger.info(f"Consolidation complete: {stats}")
    return stats


# ── GPU Server helpers ─────────────────────────────────────

def _synthesize_with_gpu_server(episodes_context: str) -> str | None:
    """Use GPU server to synthesize episodes into semantic knowledge."""
    prompt = (
        "You are consolidating several related experiences into one concise knowledge statement.\n\n"
        f"EPISODES:\n{episodes_context[:4000]}\n\n"
        "Synthesize the COMMON KNOWLEDGE from these episodes into 1-3 dense sentences. "
        "Extract the general principle, not the specific events. "
        "Format: '[semantic] <the distilled knowledge>'. No preamble."
    )
    return _call_gpu_server(prompt, max_tokens=200)


def _crystallize_with_gpu_server(skill_name: str, contents: list[str]) -> str | None:
    """Use GPU server to crystallize semantic memories into a procedure."""
    context = "\n".join(f"- {c}" for c in contents)
    prompt = (
        f"You are crystallizing knowledge about '{skill_name}' into a step-by-step procedure.\n\n"
        f"KNOWLEDGE:\n{context[:4000]}\n\n"
        "Write a concise procedure (numbered steps) for performing this skill. "
        "Include key warnings and pitfalls. Max 5 steps. No preamble."
    )
    return _call_gpu_server(prompt, max_tokens=300)


def _call_gpu_server(prompt: str, max_tokens: int = 200) -> str | None:
    """Call GPU server for text generation."""
    try:
        from brain.platform.gpu_client import get_client
        result = get_client().generate(
            prompt=prompt, max_tokens=max_tokens,
            temperature=0.3, think=False, fallback_policy="auto",
        )
        text_result = result.strip() if result else ""
        if text_result and len(text_result) > 20:
            return text_result
    except Exception as e:
        logger.debug(f"GPU server call failed: {e}")
    return None
