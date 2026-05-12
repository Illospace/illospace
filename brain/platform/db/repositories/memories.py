"""MemoryRepository — domain queries for memories, edges, tags."""
from __future__ import annotations

import logging
from typing import Any, Iterable, Sequence

from datetime import datetime, timedelta

from sqlalchemy import Integer, and_, func, or_, select, text, update
from sqlalchemy.orm import aliased, load_only

from brain.platform.db.models.memory import Edge, Memory, MemoryContradiction, MemoryReview
from brain.platform.db.models.org import User
from brain.platform.db.models.system import RetrievalLog
from brain.platform.db.repositories.base import BaseRepository
from brain.platform.db.repositories.memory_write_context import (
    MemoryWriteContextError,
    MemoryWriteContext,
    require_memory_write_context,
)
from brain.platform.db.repositories.memory_visibility import (
    MemoryVisibilityContext,
    memory_visibility_sql,
    memory_visibility_predicate,
    require_memory_visible,
)
from brain.systems.memory.truth_maintenance import (
    apply_active_truth_maintenance,
    build_truth_state,
    filter_truth_safe_memories,
    quarantine_filter_enabled,
    memory_retrieval_bonus,
    memory_retrieval_priority,
)

logger = logging.getLogger(__name__)


GRAPH_EDGE_WEIGHT_BONUS = {
    "contradicts": 0.15,
    "depends_on": 0.12,
    "derived_from": 0.10,
    "caused_by": 0.10,
    "consolidated_from": 0.08,
    "crystallized_from": 0.08,
    "similar_to": 0.05,
    "related_to": 0.03,
}


class MemoryRepository(BaseRepository[Memory]):
    model = Memory

    def create(self, **kwargs) -> Memory:
        raise MemoryWriteContextError(
            "MemoryRepository.create() is disabled; use insert_memory(context=MemoryWriteContext(...))"
        )

    def insert_memory(
        self,
        *,
        content: str,
        memory_type: str,
        context: MemoryWriteContext,
        semantic_embedding: Any | None = None,
        salience: float = 5.0,
        tags: list[str] | None = None,
        related_ids: list[int] | None = None,
        rel_type: str = "related_to",
        decay_eligible: bool = True,
        scope: str = "personal",
        memory_tier: str = "episodic",
        source_memory_ids: list[int] | None = None,
        harvest_type: str | None = None,
        harvest_confidence: float | None = None,
        topic_tags: list[str] | None = None,
        auto_edge: bool = False,
        auto_edge_k: int = 0,
        auto_edge_threshold: float = 0.5,
    ) -> dict:
        """Insert a memory row using the explicit write context.

        This is the canonical write path for CLI and MCP memory creation. It
        centralizes ownership/visibility columns and keeps auto-edge creation
        scoped to memories visible to the same writer context.
        """

        context = require_memory_write_context(context)
        tags = list(tags or [])
        source_memory_ids = list(source_memory_ids or [])
        topic_tags = list(topic_tags or [])
        confidence = context.confidence if context.confidence is not None else 0.5
        harvest_confidence = (
            max(0.0, min(1.0, float(harvest_confidence)))
            if harvest_confidence is not None
            else None
        )

        if self._dialect_name() == "sqlite":
            memory_id = self._insert_memory_orm(
                content=content,
                memory_type=memory_type,
                context=context,
                semantic_embedding=semantic_embedding,
                salience=salience,
                tags=tags,
                decay_eligible=decay_eligible,
                scope=scope,
                memory_tier=memory_tier,
                source_memory_ids=source_memory_ids,
                harvest_type=harvest_type,
                harvest_confidence=harvest_confidence,
                topic_tags=topic_tags,
                confidence=confidence,
            )
        else:
            from brain.systems.memory.embeddings import vec_to_pg

            result = self._session.execute(
                text("""
                    INSERT INTO memories (
                        content, memory_type, semantic_embedding,
                        salience,
                        source, tags, decay_eligible, source_session, scope,
                        memory_tier, source_memory_ids,
                        user_id, org_id, visibility, confidence, source_ref,
                        harvest_type, harvest_confidence, topic_tags
                    ) VALUES (
                        :content, :memory_type,
                        CAST(:semantic_embedding AS vector),
                        :salience,
                        :source, :tags, :decay_eligible, :source_session, :scope,
                        :memory_tier, :source_memory_ids,
                        :user_id, :org_id, :visibility, :confidence, :source_ref,
                        :harvest_type, :harvest_confidence, :topic_tags
                    )
                    RETURNING id
                """),
                {
                    "content": content,
                    "memory_type": memory_type,
                    "semantic_embedding": (
                        vec_to_pg(semantic_embedding)
                        if semantic_embedding is not None
                        else None
                    ),
                    "salience": salience,
                    "source": context.source,
                    "tags": tags,
                    "decay_eligible": decay_eligible,
                    "source_session": context.source_session(),
                    "scope": scope,
                    "memory_tier": memory_tier,
                    "source_memory_ids": source_memory_ids,
                    "user_id": context.user_id,
                    "org_id": context.org_id,
                    "visibility": context.visibility,
                    "confidence": confidence,
                    "source_ref": context.source_ref(),
                    "harvest_type": harvest_type,
                    "harvest_confidence": harvest_confidence,
                    "topic_tags": topic_tags,
                },
            )
            memory_id = result.scalar_one()

        neighbors = []
        auto_edges_created = 0
        if auto_edge and semantic_embedding is not None and auto_edge_k > 0:
            neighbors, auto_edges_created = self._create_auto_edges(
                memory_id=memory_id,
                semantic_embedding=semantic_embedding,
                context=context,
                limit=auto_edge_k,
                threshold=auto_edge_threshold,
            )

        manual_edges_created = 0
        if related_ids:
            manual_edges_created = self._create_manual_edges(
                memory_id=memory_id,
                related_ids=related_ids,
                rel_type=rel_type,
                context=context,
            )

        truth_maintenance: dict[str, Any] = {}
        try:
            truth_maintenance = apply_active_truth_maintenance(
                self._session,
                memory_id=memory_id,
                content=content,
                evidence=context.evidence,
                confidence=confidence,
                user_id=context.user_id,
                org_id=context.org_id,
            )
        except Exception:
            logger.debug("Active truth maintenance skipped for memory %s", memory_id, exc_info=True)

        return {
            "id": memory_id,
            "type": memory_type,
            "salience": salience,
            "visibility": context.visibility,
            "user_id": context.user_id,
            "org_id": context.org_id,
            "source": context.source,
            "source_ref": context.source_ref(),
            "auto_edges": auto_edges_created,
            "manual_edges": manual_edges_created,
            "neighbors": neighbors,
            "truth_maintenance": truth_maintenance,
        }

    def _dialect_name(self) -> str | None:
        try:
            bind = self._session.get_bind()
            return bind.dialect.name
        except Exception:
            bind = getattr(self._session, "bind", None)
            return getattr(getattr(bind, "dialect", None), "name", None)

    def _insert_memory_orm(self, **kwargs) -> int:
        context: MemoryWriteContext = kwargs.pop("context")
        memory = Memory(
            content=kwargs["content"],
            memory_type=kwargs["memory_type"],
            semantic_embedding=kwargs["semantic_embedding"],
            salience=kwargs["salience"],
            source=context.source,
            source_session=context.source_session(),
            tags=kwargs["tags"],
            decay_eligible=kwargs["decay_eligible"],
            scope=kwargs["scope"],
            memory_tier=kwargs["memory_tier"],
            source_memory_ids=kwargs["source_memory_ids"],
            user_id=context.user_id,
            org_id=context.org_id,
            visibility=context.visibility,
            confidence=kwargs["confidence"],
            source_ref=context.source_ref(),
            harvest_type=kwargs["harvest_type"],
            harvest_confidence=kwargs["harvest_confidence"],
            topic_tags=kwargs["topic_tags"],
        )
        self._session.add(memory)
        self._session.flush()
        return memory.id

    def _create_auto_edges(
        self,
        *,
        memory_id: int,
        semantic_embedding: Any,
        context: MemoryWriteContext,
        limit: int,
        threshold: float,
    ) -> tuple[list[dict], int]:
        if self._dialect_name() == "sqlite":
            return [], 0

        from brain.systems.memory.embeddings import vec_to_pg

        vis_clause, vis_params = memory_visibility_sql(
            context.as_visibility_context(),
            alias="m",
            user_param="edge_user_id",
            org_param="edge_org_id",
        )
        rows = self._session.execute(
            text(f"""
                SELECT m.id, 1 - (m.semantic_embedding <=> CAST(:embedding AS vector)) AS similarity
                FROM memories m
                WHERE m.id != :memory_id
                  AND COALESCE(m.archived, FALSE) = FALSE
                  AND m.semantic_embedding IS NOT NULL
                  {vis_clause}
                ORDER BY m.semantic_embedding <=> CAST(:embedding AS vector)
                LIMIT :limit
            """),
            {
                "embedding": vec_to_pg(semantic_embedding),
                "memory_id": memory_id,
                "limit": limit,
                **vis_params,
            },
        ).mappings().all()

        neighbors: list[dict] = []
        edges_created = 0
        for row in rows:
            similarity = float(row["similarity"] or 0.0)
            if similarity <= threshold:
                continue
            self._session.execute(
                text("""
                    INSERT INTO edges (source_id, target_id, relationship, weight, auto_generated)
                    VALUES (:source_id, :target_id, 'similar_to', :weight, TRUE)
                    ON CONFLICT (source_id, target_id, relationship)
                    DO UPDATE SET weight = :weight
                """),
                {"source_id": memory_id, "target_id": row["id"], "weight": similarity},
            )
            neighbors.append({"id": row["id"], "similarity": round(similarity, 3)})
            edges_created += 1
        return neighbors, edges_created

    def _create_manual_edges(
        self,
        *,
        memory_id: int,
        related_ids: list[int],
        rel_type: str,
        context: MemoryWriteContext,
    ) -> int:
        visibility_context = context.as_visibility_context()
        edges_created = 0
        for related_id in related_ids:
            related = self.get_visible(related_id, visibility_context)
            require_memory_visible(related, visibility_context)
            self._session.execute(
                text("""
                    INSERT INTO edges (source_id, target_id, relationship, weight)
                    VALUES (:source_id, :target_id, :relationship, 1.0)
                    ON CONFLICT (source_id, target_id, relationship) DO NOTHING
                """),
                {
                    "source_id": memory_id,
                    "target_id": related_id,
                    "relationship": rel_type,
                },
            )
            edges_created += 1
        return edges_created

    def _filter_truth_safe(self, memories: list[Memory]) -> list[Memory]:
        return filter_truth_safe_memories(memories, quarantine_filter=quarantine_filter_enabled())

    def _truth_ranked(self, memories: list[Memory], *, limit: int | None = None) -> list[Memory]:
        ranked = sorted(self._filter_truth_safe(memories), key=memory_retrieval_priority)
        if limit is not None:
            return ranked[:limit]
        return ranked

    @staticmethod
    def _coerce_ids(ids: Iterable[int]) -> list[int]:
        return [int(memory_id) for memory_id in ids]

    @staticmethod
    def _embedding_to_pg(embedding: Any) -> str:
        if isinstance(embedding, str):
            return embedding
        from brain.systems.memory.embeddings import vec_to_pg

        return vec_to_pg(embedding)

    @staticmethod
    def _truth_sql(alias: str = "m") -> str:
        if not quarantine_filter_enabled():
            return ""
        prefix = f"{alias}." if alias else ""
        return f"""
          AND COALESCE({prefix}truth_status, 'unknown') NOT IN ('quarantined', 'expired', 'superseded')
          AND COALESCE({prefix}review_status, 'unreviewed') != 'rejected'
          AND {prefix}demoted_at IS NULL
          AND ({prefix}valid_until IS NULL OR {prefix}valid_until >= NOW())
        """

    def touch_memories(self, ids: Iterable[int]) -> None:
        """Record that memories were read within the current transaction."""
        memory_ids = self._coerce_ids(ids)
        if not memory_ids:
            return
        stmt = (
            update(Memory)
            .where(Memory.id.in_(memory_ids))
            .values(
                last_accessed=func.now(),
                access_count=func.coalesce(Memory.access_count, 0) + 1,
            )
        )
        self._session.execute(stmt)

    def query_ranked(
        self,
        *,
        query_embedding: Any,
        limit: int = 5,
        memory_type: str | None = None,
        min_salience: float | None = None,
        tags: list[str] | None = None,
        context: MemoryVisibilityContext | None = None,
        spread: bool = False,
    ) -> dict[str, list[dict]]:
        """Run the legacy multi-signal pgvector memory query behind the repo."""
        qemb = self._embedding_to_pg(query_embedding)
        visibility_context = context or MemoryVisibilityContext()
        vis_clause, vis_params = memory_visibility_sql(visibility_context, alias="m")

        sql = """
            WITH ranked AS (
                SELECT
                    m.id, m.content, m.memory_type, m.salience,
                    m.tags, m.created_at, m.last_accessed, m.access_count,
                    m.memory_tier,
                    1 - (m.semantic_embedding <=> CAST(:qemb AS vector)) as semantic_score,
                    EXP(-0.05 * EXTRACT(EPOCH FROM (NOW() - m.last_accessed)) / 86400) as recency_score,
                    LN(m.access_count + 1) / 5.0 as frequency_score
                FROM memories m
                WHERE NOT m.archived
                  AND m.superseded_by IS NULL
                  AND m.semantic_embedding IS NOT NULL
        """
        params: dict[str, Any] = {
            "qemb": qemb,
            **vis_params,
        }

        if memory_type:
            sql += " AND m.memory_type = :mtype"
            params["mtype"] = memory_type
        if min_salience:
            sql += " AND m.salience >= :min_sal"
            params["min_sal"] = min_salience
        if tags:
            sql += " AND m.tags && :tags"
            params["tags"] = tags
        sql += vis_clause

        sql += """
            )
            SELECT *,
                (semantic_score * 0.35 + (salience / 10.0) * 0.25
                 + recency_score * 0.15 + frequency_score * 0.10
                 + CASE WHEN memory_tier = 'procedural' THEN 0.10
                        WHEN memory_tier = 'semantic' THEN 0.05
                        ELSE 0.0 END
                ) as combined_score
            FROM ranked ORDER BY combined_score DESC LIMIT :limit
        """
        params["limit"] = limit
        rows = [dict(row) for row in self._session.execute(text(sql), params).mappings().all()]
        self.touch_memories(row["id"] for row in rows)

        spread_rows: list[dict] = []
        if spread and rows:
            spread_rows = self.spread_activation(
                [row["id"] for row in rows[:3]],
                context=visibility_context,
                limit=5,
            )
        return {"results": rows, "spread_activation": spread_rows}

    def retrieve_with_pools(
        self,
        *,
        query_embedding: Any,
        limit: int = 5,
        org_id: str | None = None,
        user_id: str | None = None,
        ratios: dict[str, float] | None = None,
    ) -> list[dict]:
        """Run three-pool memory retrieval within this repository session."""
        from brain.systems.memory.retrieval_pools import PoolRetriever, RetrievalConfig

        ratios = ratios or {}
        cfg = RetrievalConfig(
            total_results=limit,
            exploit_ratio=ratios.get("recency", 0.60),
            explore_ratio=ratios.get("semantic", 0.25),
            narrative_ratio=ratios.get("narrative", 0.15),
            org_id=org_id,
            user_id=user_id,
        )
        return PoolRetriever(cfg).retrieve(
            query_embedding=list(query_embedding),
            session=self._session,
            ratios=None,
        )

    def spread_activation(
        self,
        seed_ids: Iterable[int],
        *,
        context: MemoryVisibilityContext | None = None,
        limit: int = 5,
    ) -> list[dict]:
        """Return immediate graph neighbors for already-selected memories."""
        ids = self._coerce_ids(seed_ids)
        if not ids:
            return []
        visibility_context = context or MemoryVisibilityContext()
        vis_clause, vis_params = memory_visibility_sql(
            visibility_context,
            alias="m",
            user_param="spread_user_id",
            org_param="spread_org_id",
        )
        rows = self._session.execute(
            text(f"""
                SELECT DISTINCT m.id, m.content, m.memory_type, m.salience,
                       e.relationship, e.weight as edge_weight, e.source_id as from_id
                FROM edges e
                JOIN memories m ON m.id = CASE WHEN e.source_id = ANY(:ids) THEN e.target_id ELSE e.source_id END
                WHERE (e.source_id = ANY(:ids) OR e.target_id = ANY(:ids))
                  AND m.id != ALL(:ids) AND NOT m.archived
                  {vis_clause}
                ORDER BY e.weight DESC LIMIT :limit
            """),
            {"ids": ids, "limit": limit, **vis_params},
        ).mappings().all()
        return [dict(row) for row in rows]

    def recall_vector(
        self,
        *,
        query_embedding: Any,
        limit: int = 3,
        context: MemoryVisibilityContext | None = None,
        min_similarity: float = 0.45,
    ) -> list[dict]:
        """Pure vector fallback used by MCP recall."""
        emb = self._embedding_to_pg(query_embedding)
        visibility_context = context or MemoryVisibilityContext()
        vis_clause, vis_params = memory_visibility_sql(visibility_context, alias="")
        rows = self._session.execute(text(f"""
            SELECT id, content, memory_type, salience,
                   COALESCE(visibility, 'private') as visibility,
                   1 - (semantic_embedding <=> CAST(:emb1 AS vector)) as similarity
            FROM memories
            WHERE NOT archived
              AND 1 - (semantic_embedding <=> CAST(:emb2 AS vector)) > :min_similarity
              {vis_clause}
            ORDER BY
                CASE WHEN memory_type IN ('lesson', 'pattern') THEN 0.15 ELSE 0 END
                + (1 - (semantic_embedding <=> CAST(:emb3 AS vector))) * 0.7
                + (salience / 10.0) * 0.15
                DESC
            LIMIT :lim
        """), {
            "emb1": emb,
            "emb2": emb,
            "emb3": emb,
            "min_similarity": min_similarity,
            "lim": limit,
            **vis_params,
        }).mappings().all()

        memories = []
        for row in rows:
            similarity = float(row["similarity"] or 0)
            if similarity > 0.5:
                memories.append({
                    "id": row["id"],
                    "content": row["content"][:300],
                    "type": row["memory_type"],
                    "salience": float(row["salience"]) if row["salience"] else 0,
                    "similarity": round(similarity, 3),
                    "visibility": row.get("visibility", "private"),
                })
        self.touch_memories(memory["id"] for memory in memories)
        return memories

    def graph_augmented_recall(
        self,
        *,
        query_embedding: Any,
        limit: int = 5,
        hops: int = 1,
        context: MemoryVisibilityContext | None = None,
    ) -> list[dict]:
        """Vector search plus scoped graph traversal for richer recall."""
        del hops  # Current graph recall is intentionally one-hop.
        emb = self._embedding_to_pg(query_embedding)
        visibility_context = context or MemoryVisibilityContext()
        truth_clause = self._truth_sql("m")
        vis_clause, vis_params = memory_visibility_sql(visibility_context, alias="m")
        params: dict[str, Any] = {"emb": emb, "pool_size": limit * 3, **vis_params}

        vector_results = self._session.execute(text(f"""
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

        seen: dict[int, dict] = {}
        for row in vector_results:
            state = build_truth_state(row)
            if quarantine_filter_enabled() and not state["is_active"]:
                continue
            seen[row["id"]] = {
                "id": row["id"],
                "content": row["content"][:300],
                "type": row["memory_type"],
                "tier": row["memory_tier"],
                "salience": float(row["salience"]) if row["salience"] else 0,
                "similarity": round(float(row["similarity"] or 0), 3),
                "visibility": row.get("visibility", "private"),
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
                "priority": memory_retrieval_priority(row),
                "score": float(row["similarity"] or 0) + memory_retrieval_bonus(row),
            }

        if not seen:
            return []

        seed_ids = list(seen.keys())[:limit]
        graph_params: dict[str, Any] = {"seed_ids": seed_ids, "graph_limit": limit * 2}
        graph_vis, graph_vis_params = memory_visibility_sql(
            visibility_context,
            alias="m",
            user_param="gvis_user_id",
            org_param="gvis_org_id",
        )
        graph_params.update(graph_vis_params)
        graph_results = self._session.execute(text(f"""
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

        for row in graph_results:
            connected_id = row["connected_id"]
            relationship = row["relationship"]
            bonus = GRAPH_EDGE_WEIGHT_BONUS.get(relationship, 0.03)
            state = build_truth_state(row)
            if quarantine_filter_enabled() and not state["is_active"]:
                continue
            from_id = row["source_id"] if row["source_id"] in seed_ids else row["target_id"]
            retrieval_bonus = memory_retrieval_bonus(row)
            priority = memory_retrieval_priority(row)

            if connected_id in seen:
                seen[connected_id]["score"] += bonus * float(row["weight"]) + retrieval_bonus
                seen[connected_id]["priority"] = min(seen[connected_id]["priority"], priority)
                seen[connected_id]["truth_status"] = state["truth_status"]
                seen[connected_id]["review_status"] = state["review_status"]
                seen[connected_id]["confidence"] = round(float(state["confidence"] or 0.5), 3)
                seen[connected_id]["freshness_score"] = round(float(state["freshness_score"] or 0.5), 3)
                seen[connected_id]["policy_kind"] = state.get("policy_kind")
                seen[connected_id]["policy_scope"] = state.get("policy_scope")
                seen[connected_id]["contradiction_status"] = state["contradiction_status"]
                seen[connected_id]["is_reviewed_active"] = state["is_reviewed_active"]
                seen[connected_id]["is_policy_effective"] = state["is_policy_effective"]
                seen[connected_id]["graph_edges"].append({
                    "relationship": relationship,
                    "from_memory": from_id,
                    "weight": round(float(row["weight"]), 3),
                })
                continue

            seen[connected_id] = {
                "id": connected_id,
                "content": row["content"][:300],
                "type": row["memory_type"],
                "tier": row["memory_tier"],
                "salience": float(row["salience"]) if row["salience"] else 0,
                "similarity": 0.0,
                "visibility": row.get("visibility", "private"),
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
                    "relationship": relationship,
                    "from_memory": from_id,
                    "weight": round(float(row["weight"]), 3),
                }],
                "priority": priority,
                "score": bonus * float(row["weight"]) + (float(row["salience"] or 0) / 10.0) * 0.1 + retrieval_bonus,
            }

        ranked = sorted(seen.values(), key=lambda item: (item["priority"], -item["score"]))
        results = ranked[:limit]
        result_ids = [item["id"] for item in results]
        self.touch_memories(result_ids)
        EdgeRepository(self._session).activate_between(result_ids)
        for item in results:
            item.pop("priority", None)
        return results

    @staticmethod
    def _active_predicate():
        return or_(Memory.archived == False, Memory.archived.is_(None))  # noqa: E712

    def _visible_active_stmt(self, context: MemoryVisibilityContext):
        return select(Memory).where(
            self._active_predicate(),
            memory_visibility_predicate(Memory, context),
        )

    def list_active(self, *, limit: int | None = 500) -> Sequence[Memory]:
        stmt = (
            select(Memory)
            .where(or_(Memory.archived == False, Memory.archived.is_(None)))  # noqa: E712
            .order_by(Memory.salience.desc())
        )
        if limit:
            stmt = stmt.limit(limit * 2)
        memories = self._session.scalars(stmt).all()
        return self._truth_ranked(memories, limit=limit)

    def list_visible(
        self,
        context: MemoryVisibilityContext,
        *,
        limit: int | None = 500,
    ) -> Sequence[Memory]:
        stmt = self._visible_active_stmt(context).order_by(Memory.salience.desc())
        if limit:
            stmt = stmt.limit(limit * 2)
        memories = self._session.scalars(stmt).all()
        return self._truth_ranked(memories, limit=limit)

    def list_by_type(self, memory_type: str) -> Sequence[Memory]:
        stmt = (
            select(Memory)
            .where(
                Memory.memory_type == memory_type,
                or_(Memory.archived == False, Memory.archived.is_(None)),  # noqa: E712
            )
            .order_by(Memory.salience.desc())
        )
        memories = self._session.scalars(stmt).all()
        return self._truth_ranked(memories)

    def search(self, query: str, *, limit: int = 50) -> Sequence[Memory]:
        stmt = (
            select(Memory)
            .where(
                Memory.content.ilike(f"%{query}%"),
                or_(Memory.archived == False, Memory.archived.is_(None)),  # noqa: E712
            )
            .order_by(Memory.salience.desc())
        )
        stmt = stmt.limit(limit * 2)
        memories = self._session.scalars(stmt).all()
        return self._truth_ranked(memories, limit=limit)

    def search_visible(
        self,
        query: str,
        context: MemoryVisibilityContext,
        *,
        limit: int = 50,
    ) -> Sequence[Memory]:
        stmt = (
            self._visible_active_stmt(context)
            .where(Memory.content.ilike(f"%{query}%"))
            .order_by(Memory.salience.desc())
            .limit(limit * 2)
        )
        memories = self._session.scalars(stmt).all()
        return self._truth_ranked(memories, limit=limit)

    def list_filtered(
        self,
        *,
        memory_type: str | None = None,
        limit: int = 20,
        min_salience: float | None = None,
        tags: list[str] | None = None,
        context: MemoryVisibilityContext | None = None,
    ) -> Sequence[Memory]:
        stmt = select(Memory).where(self._active_predicate())
        if context:
            stmt = stmt.where(memory_visibility_predicate(Memory, context))
        if memory_type:
            stmt = stmt.where(Memory.memory_type == memory_type)
        if min_salience:
            stmt = stmt.where(Memory.salience >= min_salience)
        if tags:
            stmt = stmt.where(Memory.tags.overlap(tags))
        stmt = stmt.order_by(Memory.salience.desc(), Memory.created_at.desc()).limit(limit)
        return self._session.scalars(stmt).all()

    def list_stale(
        self, *, days: int = 90, min_access: int = 3
    ) -> Sequence[Memory]:
        from datetime import datetime, timedelta, timezone

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = (
            select(Memory)
            .where(
                Memory.created_at < cutoff,
                Memory.access_count < min_access,
                or_(Memory.archived == False, Memory.archived.is_(None)),  # noqa: E712
            )
            .order_by(Memory.access_count.asc())
        )
        memories = self._session.scalars(stmt).all()
        return self._truth_ranked(memories)

    def list_stale_visible(
        self,
        context: MemoryVisibilityContext,
        *,
        days: int = 90,
        min_access: int = 3,
    ) -> Sequence[Memory]:
        from datetime import datetime, timedelta, timezone

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = (
            self._visible_active_stmt(context)
            .where(
                Memory.created_at < cutoff,
                Memory.access_count < min_access,
            )
            .order_by(Memory.access_count.asc())
        )
        memories = self._session.scalars(stmt).all()
        return self._truth_ranked(memories)

    def get_visible(self, memory_id: int, context: MemoryVisibilityContext) -> Memory | None:
        stmt = select(Memory).where(
            Memory.id == memory_id,
            memory_visibility_predicate(Memory, context),
        )
        return self._session.scalars(stmt).first()

    def get_or_raise_visible(self, memory_id: int, context: MemoryVisibilityContext) -> Memory:
        memory = self.get_visible(memory_id, context)
        if memory is None:
            raise LookupError(f"Memory {memory_id} not found")
        return memory

    def get_detail(
        self,
        memory_id: int,
        *,
        context: MemoryVisibilityContext | None = None,
    ) -> dict | None:
        memory = self.get_visible(memory_id, context) if context else self.get(memory_id)
        if not memory:
            return None
        return {
            "memory": memory,
            "edges": self.connected_edge_rows(memory_id, context=context),
        }

    def connected_edge_rows(
        self,
        memory_id: int,
        *,
        context: MemoryVisibilityContext | None = None,
    ) -> list[dict]:
        vis_clause = ""
        vis_params: dict[str, str] = {}
        if context:
            vis_clause, vis_params = memory_visibility_sql(
                context,
                alias="m",
                user_param="detail_user_id",
                org_param="detail_org_id",
            )
        rows = self._session.execute(
            text(f"""
                SELECT e.*,
                       CASE WHEN e.source_id = :mid THEN e.target_id ELSE e.source_id END as connected_id,
                       m.content as connected_content, m.memory_type as connected_type
                FROM edges e
                JOIN memories m ON m.id = CASE WHEN e.source_id = :mid THEN e.target_id ELSE e.source_id END
                WHERE (e.source_id = :mid OR e.target_id = :mid)
                  {vis_clause}
                ORDER BY e.weight DESC
            """),
            {"mid": memory_id, **vis_params},
        ).mappings().all()
        return [dict(row) for row in rows]

    def get_graph_context(
        self,
        memory_id: int,
        *,
        depth: int = 2,
        context: MemoryVisibilityContext | None = None,
    ) -> dict:
        """Return a scoped recursive graph neighborhood for a memory."""
        vis_clause = ""
        vis_params: dict[str, str] = {}
        if context:
            vis_clause, vis_params = memory_visibility_sql(
                context,
                alias="m",
                user_param="context_user_id",
                org_param="context_org_id",
            )
        nodes = self._session.execute(
            text(f"""
                WITH RECURSIVE neighborhood AS (
                    SELECT m.id, m.content, m.memory_type, m.salience,
                           0 as depth, ARRAY[m.id] as path
                    FROM memories m
                    WHERE m.id = :sid
                      {vis_clause}
                    UNION ALL
                    SELECT m.id, m.content, m.memory_type, m.salience,
                           n.depth + 1, n.path || m.id
                    FROM neighborhood n
                    JOIN edges e ON (e.source_id = n.id OR e.target_id = n.id)
                    JOIN memories m ON m.id = CASE WHEN e.source_id = n.id THEN e.target_id ELSE e.source_id END
                    WHERE n.depth < :depth AND NOT m.id = ANY(n.path) AND NOT m.archived
                      {vis_clause}
                )
                SELECT DISTINCT ON (id) * FROM neighborhood ORDER BY id, depth ASC
            """),
            {"sid": memory_id, "depth": depth, **vis_params},
        ).mappings().all()

        node_ids = [row["id"] for row in nodes]
        edge_rows: list[dict] = []
        if node_ids:
            edge_rows = [dict(row) for row in self._session.execute(
                text("""
                    SELECT source_id, target_id, relationship, weight, auto_generated
                    FROM edges WHERE source_id = ANY(:ids) AND target_id = ANY(:ids)
                """),
                {"ids": node_ids},
            ).mappings().all()]

        return {
            "center": memory_id,
            "depth": depth,
            "nodes": [
                {
                    "id": row["id"],
                    "content": row["content"],
                    "type": row["memory_type"],
                    "salience": row["salience"],
                    "distance": row["depth"],
                }
                for row in nodes
            ],
            "edges": [
                {
                    "source": row["source_id"],
                    "target": row["target_id"],
                    "relationship": row["relationship"],
                    "weight": round(row["weight"], 3),
                    "auto": row["auto_generated"],
                }
                for row in edge_rows
            ],
        }

    def get_memory_neighborhood(
        self,
        memory_id: int,
        *,
        hops: int = 1,
        context: MemoryVisibilityContext | None = None,
    ) -> dict:
        """Return the one-hop neighborhood shape used by cognition.graph."""
        del hops
        center_vis = ""
        center_params: dict[str, str] = {}
        neighbor_vis = ""
        neighbor_params: dict[str, str] = {}
        if context:
            center_vis, center_params = memory_visibility_sql(
                context,
                alias="",
                user_param="center_user_id",
                org_param="center_org_id",
            )
            neighbor_vis, neighbor_params = memory_visibility_sql(
                context,
                alias="m",
                user_param="neighbor_user_id",
                org_param="neighbor_org_id",
            )
        center = self._session.execute(text(f"""
            SELECT id, content, memory_type, salience,
                   COALESCE(memory_tier, 'episodic') as memory_tier,
                   COALESCE(truth_status, 'unknown') as truth_status,
                   COALESCE(review_status, 'unreviewed') as review_status,
                   COALESCE(confidence, 0.5) as confidence
            FROM memories WHERE id = :mem_id
            {center_vis}
        """), {"mem_id": memory_id, **center_params}).mappings().first()

        if not center:
            return {"error": f"Memory {memory_id} not found"}

        neighbors = self._session.execute(text(f"""
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
        """), {"mem_id": memory_id, **neighbor_params}).mappings().all()

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
                "relationship": row["relationship"],
                "direction": row["direction"],
                "weight": round(float(row["weight"]), 3),
                "memory": {
                    "id": row["id"],
                    "content": row["content"][:200],
                    "type": row["memory_type"],
                    "tier": row["memory_tier"],
                    "truth_status": row.get("truth_status", "unknown"),
                    "review_status": row.get("review_status", "unreviewed"),
                },
            } for row in neighbors],
            "edge_count": len(neighbors),
        }

    def count_active(self) -> int:
        """Count non-archived memories."""
        if not quarantine_filter_enabled():
            stmt = select(func.count(Memory.id)).where(
                or_(Memory.archived == False, Memory.archived.is_(None))  # noqa: E712
            )
            return self._session.scalar(stmt) or 0
        return len(self.list_active(limit=None))

    def count_archived(self) -> int:
        """Count archived memories."""
        stmt = select(func.count(Memory.id)).where(Memory.archived == True)  # noqa: E712
        return self._session.scalar(stmt) or 0

    def count_by_type(self) -> dict[str, int]:
        """Count memories grouped by memory_type."""
        if not quarantine_filter_enabled():
            stmt = (
                select(Memory.memory_type, func.count(Memory.id))
                .where(or_(Memory.archived == False, Memory.archived.is_(None)))  # noqa: E712
                .group_by(Memory.memory_type)
            )
            rows = self._session.execute(stmt).all()
            return {row[0]: row[1] for row in rows}

        counts: dict[str, int] = {}
        for memory in self.list_active(limit=None):
            counts[memory.memory_type] = counts.get(memory.memory_type, 0) + 1
        return counts

    def stats(self) -> dict:
        """Return the CLI/API memory statistics snapshot."""
        total = self._session.scalar(select(func.count(Memory.id))) or 0
        archived = self._session.scalar(
            select(func.count(Memory.id)).where(Memory.archived == True)  # noqa: E712
        ) or 0
        edge_total = self._session.scalar(select(func.count(Edge.id))) or 0
        auto_edges = self._session.scalar(
            select(func.count(Edge.id)).where(Edge.auto_generated == True)  # noqa: E712
        ) or 0
        by_type_rows = self._session.execute(
            select(Memory.memory_type, func.count(Memory.id), func.avg(Memory.salience))
            .where(self._active_predicate())
            .group_by(Memory.memory_type)
            .order_by(func.count(Memory.id).desc())
        ).all()
        return {
            "memories": {"total": total, "archived": archived, "active": total - archived},
            "edges": {"total": edge_total, "auto_generated": auto_edges},
            "by_type": [
                {
                    "memory_type": row[0],
                    "count": row[1],
                    "avg_salience": round(float(row[2] or 0), 1),
                }
                for row in by_type_rows
            ],
        }

    def list_org_memories(
        self,
        context: MemoryVisibilityContext,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[Memory]:
        if not context.org_id:
            return []
        stmt = (
            select(Memory)
            .where(
                Memory.visibility == "org",
                Memory.org_id == context.org_id,
                self._active_predicate(),
            )
            .order_by(Memory.salience.desc())
            .offset(offset)
            .limit(limit)
        )
        return self._session.scalars(stmt).all()

    def list_decay_candidates(self, *, days: int = 30, threshold: float = 2.0) -> Sequence[Memory]:
        cutoff = datetime.now() - timedelta(days=days)
        stmt = (
            select(Memory)
            .where(
                Memory.decay_eligible == True,  # noqa: E712
                self._active_predicate(),
                Memory.last_accessed < cutoff,
                Memory.salience < threshold,
            )
            .order_by(Memory.salience.asc(), Memory.last_accessed.asc())
        )
        return self._session.scalars(stmt).all()

    def archive_many(self, ids: Iterable[int]) -> int:
        memory_ids = self._coerce_ids(ids)
        if not memory_ids:
            return 0
        result = self._session.execute(
            update(Memory).where(Memory.id.in_(memory_ids)).values(archived=True)
        )
        return int(result.rowcount or 0)

    def list_index_memories(self, *, limit: int = 50) -> Sequence[Memory]:
        stmt = (
            select(Memory)
            .where(
                self._active_predicate(),
                Memory.superseded_by.is_(None),
                Memory.salience >= 5,
            )
            .order_by(Memory.salience.desc(), Memory.last_accessed.desc())
            .limit(limit)
        )
        return self._session.scalars(stmt).all()

    def high_salience_warnings_for_skill(
        self,
        *,
        skill_embedding: Any,
        limit: int = 3,
    ) -> list[str]:
        emb = self._embedding_to_pg(skill_embedding)
        rows = self._session.execute(text("""
            SELECT content, salience
            FROM memories
            WHERE memory_type IN ('lesson', 'pattern')
              AND salience >= 9
              AND NOT archived
              AND 1 - (semantic_embedding <=> CAST(:emb AS vector)) > 0.5
            ORDER BY salience DESC
            LIMIT :limit
        """), {"emb": emb, "limit": limit}).mappings().all()
        return [row["content"][:300] for row in rows]

    def guardrail_memories_for_task(
        self,
        *,
        task_embedding: Any,
        limit: int = 5,
    ) -> list[dict]:
        emb = self._embedding_to_pg(task_embedding)
        rows = self._session.execute(text("""
            SELECT content, memory_type, salience,
                   1 - (semantic_embedding <=> CAST(:emb1 AS vector)) as similarity
            FROM memories
            WHERE NOT archived AND superseded_by IS NULL
              AND memory_type IN ('lesson', 'pattern')
              AND 1 - (semantic_embedding <=> CAST(:emb2 AS vector)) > 0.4
            ORDER BY semantic_embedding <=> CAST(:emb3 AS vector)
            LIMIT :limit
        """), {"emb1": emb, "emb2": emb, "emb3": emb, "limit": limit}).mappings().all()
        return [dict(row) for row in rows]

    def add_attribution(self, memories: list[dict], current_user_id: str) -> list[dict]:
        """Annotate cross-user shared memories with allowed attribution."""
        memory_ids = [memory["id"] for memory in memories]
        if not memory_ids:
            return memories
        stmt = (
            select(
                Memory.id.label("id"),
                Memory.user_id.label("user_id"),
                User.name.label("name"),
                User.attribution_enabled.label("attribution_enabled"),
            )
            .outerjoin(User, User.id == Memory.user_id)
            .where(Memory.id.in_(memory_ids))
        )
        rows = self._session.execute(stmt).mappings().all()
        user_map = {row["id"]: row for row in rows}
        for memory in memories:
            info = user_map.get(memory["id"])
            if info and info["user_id"] and str(info["user_id"]) != current_user_id:
                if info.get("attribution_enabled", True) and info.get("name"):
                    memory["attributed_to"] = info["name"]
                else:
                    memory["attributed_to"] = "A teammate"
        return memories

    def recent_activity(self, *, limit: int = 20) -> list[dict]:
        """Return recent memories as activity feed items."""
        stmt = (
            select(Memory)
            .options(
                load_only(
                    Memory.id,
                    Memory.content,
                    Memory.memory_type,
                    Memory.created_at,
                    Memory.archived,
                    Memory.memory_tier,
                    Memory.truth_status,
                    Memory.review_status,
                    Memory.confidence,
                    Memory.freshness_score,
                    Memory.superseded_by,
                    Memory.valid_until,
                    Memory.policy_kind,
                    Memory.policy_scope,
                    Memory.demoted_at,
                )
            )
            .where(or_(Memory.archived == False, Memory.archived.is_(None)))  # noqa: E712
            .order_by(Memory.created_at.desc())
        )
        memories = self._filter_truth_safe(self._session.scalars(stmt.limit(limit * 2)).all())[:limit]
        return [
            {
                "type": "memory",
                "subtype": m.memory_type,
                "detail": ((m.content or "")[:120] + "...") if len(m.content or "") > 120 else (m.content or ""),
                "ts": m.created_at.isoformat() if m.created_at else None,
            }
            for m in memories
        ]

    def retrieval_accuracy(self) -> float | None:
        """Average retrieval accuracy from retrieval_log (where was_relevant is set)."""
        stmt = select(func.avg(
            func.cast(RetrievalLog.was_relevant, Integer)
        )).where(RetrievalLog.was_relevant.isnot(None))
        result = self._session.scalar(stmt)
        return float(result) if result is not None else None

    def get_graph_data(self, *, limit: int = 500, context: MemoryVisibilityContext | None = None) -> dict:
        memories = self.list_visible(context, limit=limit) if context else self.list_active(limit=limit)
        memory_ids = {m.id for m in memories}
        if not memory_ids:
            edges = []
        else:
            edges = self._session.scalars(
                select(Edge).where(
                    Edge.source_id.in_(memory_ids),
                    Edge.target_id.in_(memory_ids),
                )
            ).all()
        return {"nodes": memories, "edges": edges}

    def list_contradictions(
        self,
        memory_id: int | None = None,
        *,
        limit: int = 50,
    ) -> Sequence[MemoryContradiction]:
        stmt = select(MemoryContradiction).order_by(MemoryContradiction.created_at.desc())
        if memory_id is not None:
            stmt = stmt.where(
                or_(
                    MemoryContradiction.left_memory_id == memory_id,
                    MemoryContradiction.right_memory_id == memory_id,
                )
            )
        stmt = stmt.limit(limit)
        return self._session.scalars(stmt).all()

    def list_reviews(
        self,
        memory_id: int | None = None,
        *,
        limit: int = 50,
    ) -> Sequence[MemoryReview]:
        stmt = select(MemoryReview).order_by(MemoryReview.created_at.desc())
        if memory_id is not None:
            stmt = stmt.where(MemoryReview.memory_id == memory_id)
        stmt = stmt.limit(limit)
        return self._session.scalars(stmt).all()

    def get_truth_snapshot(
        self,
        memory_id: int,
        *,
        include_records: bool = False,
        context: MemoryVisibilityContext | None = None,
    ) -> dict:
        memory = self.get_or_raise_visible(memory_id, context) if context else self.get_or_raise(memory_id)
        contradictions = self.list_contradictions(memory_id) if include_records else []
        reviews = self.list_reviews(memory_id) if include_records else []
        open_contradictions = [
            contradiction
            for contradiction in contradictions
            if str(getattr(contradiction, "status", "open")).lower() not in {"resolved", "closed", "dismissed", "accepted"}
        ]
        resolved_contradictions = [c for c in contradictions if c not in open_contradictions]
        state = build_truth_state(
            {
                **getattr(memory, "__dict__", {}),
                "open_contradiction_count": len(open_contradictions),
                "resolved_contradiction_count": len(resolved_contradictions),
                "contradiction_status": "open"
                if open_contradictions
                else "resolved"
                if resolved_contradictions
                else "none",
            }
        )
        return {
            "memory": memory,
            "state": state,
            "contradictions": contradictions,
            "reviews": reviews,
            "conservative_filter_enabled": quarantine_filter_enabled(),
        }

    def get_similarity_edges(
        self,
        *,
        limit: int = 500,
        top_k: int = 5,
        threshold: float = 0.40,
        context: MemoryVisibilityContext | None = None,
    ) -> list[dict]:
        """Compute top-K most similar memory pairs using pgvector cosine distance.

        Returns list of {source_id, target_id, similarity} dicts.
        Only considers memories that have semantic_embedding set.
        """
        from sqlalchemy import text
        from brain.platform.db.repositories.memory_visibility import memory_visibility_sql

        vis_clause, vis_params = memory_visibility_sql(context, alias="", user_param="user_id", org_param="org_id") if context else ("", {})

        # Use a CTE to get active memories with embeddings, then
        # compute pairwise cosine similarity using pgvector <=> operator.
        # We use LATERAL join to get top-K per memory efficiently.
        raw_sql = text(f"""
            WITH active_mems AS (
                SELECT id, semantic_embedding
                FROM memories
                WHERE (archived IS NULL OR archived = false)
                  AND semantic_embedding IS NOT NULL
                  {vis_clause}
                ORDER BY salience DESC
                LIMIT :mem_limit
            )
            SELECT DISTINCT ON (LEAST(a.id, b.id), GREATEST(a.id, b.id))
                a.id AS source_id,
                b.id AS target_id,
                1 - (a.semantic_embedding <=> b.semantic_embedding) AS similarity
            FROM active_mems a
            CROSS JOIN LATERAL (
                SELECT id, semantic_embedding
                FROM active_mems b
                WHERE b.id != a.id
                ORDER BY a.semantic_embedding <=> b.semantic_embedding
                LIMIT :top_k
            ) b
            WHERE 1 - (a.semantic_embedding <=> b.semantic_embedding) >= :threshold
            ORDER BY LEAST(a.id, b.id), GREATEST(a.id, b.id),
                     1 - (a.semantic_embedding <=> b.semantic_embedding) DESC
        """)

        rows = self._session.execute(
            raw_sql,
            {"mem_limit": limit, "top_k": top_k, "threshold": threshold, **vis_params},
        ).fetchall()

        return [
            {"source_id": row[0], "target_id": row[1], "similarity": float(row[2])}
            for row in rows
        ]


class EdgeRepository(BaseRepository[Edge]):
    model = Edge

    def count_all(self) -> int:
        """Count all edges."""
        stmt = select(func.count(Edge.id))
        return self._session.scalar(stmt) or 0

    def upsert_edge(
        self,
        source_id: int,
        target_id: int,
        relationship: str,
        *,
        weight: float = 1.0,
        auto_generated: bool = False,
    ) -> int:
        """Create or update an edge inside the active UnitOfWork."""
        if self._dialect_name() == "sqlite":
            stmt = select(Edge).where(
                Edge.source_id == source_id,
                Edge.target_id == target_id,
                Edge.relationship == relationship,
            )
            edge = self._session.scalars(stmt).first()
            if edge:
                edge.weight = weight
                edge.last_activated = datetime.utcnow()
            else:
                edge = Edge(
                    source_id=source_id,
                    target_id=target_id,
                    relationship=relationship,
                    weight=weight,
                    auto_generated=auto_generated,
                )
                self._session.add(edge)
            self._session.flush()
            return edge.id

        result = self._session.execute(
            text("""
                INSERT INTO edges (source_id, target_id, relationship, weight, auto_generated)
                VALUES (:source_id, :target_id, :relationship, :weight, :auto_generated)
                ON CONFLICT (source_id, target_id, relationship)
                DO UPDATE SET weight = EXCLUDED.weight, last_activated = NOW()
                RETURNING id
            """),
            {
                "source_id": source_id,
                "target_id": target_id,
                "relationship": relationship,
                "weight": weight,
                "auto_generated": auto_generated,
            },
        )
        return result.scalar_one()

    def activate_between(self, memory_ids: Iterable[int]) -> None:
        ids = [int(memory_id) for memory_id in memory_ids]
        if len(ids) < 2:
            return
        stmt = (
            update(Edge)
            .where(
                Edge.source_id.in_(ids),
                Edge.target_id.in_(ids),
            )
            .values(
                last_activated=func.now(),
                activation_count=func.coalesce(Edge.activation_count, 0) + 1,
            )
        )
        self._session.execute(stmt)

    def _dialect_name(self) -> str | None:
        try:
            bind = self._session.get_bind()
            return bind.dialect.name
        except Exception:
            bind = getattr(self._session, "bind", None)
            return getattr(getattr(bind, "dialect", None), "name", None)

    def list_by_memory(self, memory_id: int) -> Sequence[Edge]:
        stmt = select(Edge).where(
            or_(Edge.source_id == memory_id, Edge.target_id == memory_id)
        )
        return self._session.scalars(stmt).all()

    def list_by_memory_visible(
        self,
        memory_id: int,
        context: MemoryVisibilityContext,
    ) -> Sequence[Edge]:
        center = self._session.get(Memory, memory_id)
        require_memory_visible(center, context)
        source = aliased(Memory)
        target = aliased(Memory)
        stmt = (
            select(Edge)
            .join(source, Edge.source_id == source.id)
            .join(target, Edge.target_id == target.id)
            .where(
                or_(Edge.source_id == memory_id, Edge.target_id == memory_id),
                memory_visibility_predicate(source, context),
                memory_visibility_predicate(target, context),
            )
        )
        return self._session.scalars(stmt).all()

    def neighborhood(
        self,
        memory_id: int,
        *,
        depth: int = 1,
        context: MemoryVisibilityContext | None = None,
    ) -> Sequence[Edge]:
        if context:
            return self.list_by_memory_visible(memory_id, context)
        return self.list_by_memory(memory_id)
