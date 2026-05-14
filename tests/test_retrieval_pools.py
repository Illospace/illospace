"""Tests for brain.systems.memory.dedup and brain.systems.memory.retrieval_pools.

Covers result set deduplication (Task 7) and three-pool retrieval
orchestration logic (Task 8).
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from brain.platform.db.enums import PoolName
from brain.systems.memory.dedup import _cosine_similarity, deduplicate_results
from brain.systems.memory.retrieval_pools import (
    PoolRetriever,
    RetrievalConfig,
    _creation_freshness,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_result(content: str, embedding: list[float] | None, salience: float = 5.0) -> dict:
    return {
        "content": content,
        "embedding": embedding,
        "salience": salience,
    }


def _similar_embedding(base: list[float], noise: float = 0.01) -> list[float]:
    """Create an embedding very similar to *base* (cosine > 0.99)."""
    rng = np.random.default_rng(42)
    arr = np.array(base) + rng.normal(0, noise, len(base))
    return arr.tolist()


# ---------------------------------------------------------------------------
# Cosine similarity
# ---------------------------------------------------------------------------


class TestCosineSimilarity:
    def test_identical_vectors(self):
        v = [1.0, 0.0, 0.0]
        assert _cosine_similarity(v, v) == pytest.approx(1.0)

    def test_orthogonal_vectors(self):
        assert _cosine_similarity([1, 0, 0], [0, 1, 0]) == pytest.approx(0.0)

    def test_none_embedding(self):
        assert _cosine_similarity(None, [1, 0, 0]) is None
        assert _cosine_similarity([1, 0, 0], None) is None
        assert _cosine_similarity(None, None) is None

    def test_empty_embedding(self):
        assert _cosine_similarity([], [1, 0]) is None

    def test_zero_vector(self):
        assert _cosine_similarity([0, 0, 0], [1, 0, 0]) is None


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

BASE_EMB = [0.5, 0.3, 0.8, 0.1, 0.6]


class TestDeduplicateResults:
    def test_near_duplicates_removed_keep_higher_salience(self):
        r1 = _make_result("memory A", BASE_EMB, salience=8.0)
        r2 = _make_result("memory A (rephrased)", _similar_embedding(BASE_EMB), salience=3.0)
        r3 = _make_result("unrelated", [0.0, 1.0, 0.0, 0.0, 0.0], salience=5.0)

        deduped = deduplicate_results([r1, r2, r3], threshold=0.85)

        contents = [r["content"] for r in deduped]
        assert "memory A" in contents  # higher salience kept
        assert "memory A (rephrased)" not in contents  # lower salience dropped
        assert "unrelated" in contents  # unrelated kept

    def test_all_unique_unchanged(self):
        results = [
            _make_result("fact 1", [1, 0, 0, 0, 0], salience=5.0),
            _make_result("fact 2", [0, 1, 0, 0, 0], salience=5.0),
            _make_result("fact 3", [0, 0, 1, 0, 0], salience=5.0),
        ]
        deduped = deduplicate_results(results, threshold=0.85)
        assert len(deduped) == 3

    def test_empty_input(self):
        assert deduplicate_results([]) == []

    def test_single_item(self):
        r = _make_result("only one", [1, 0, 0], salience=5.0)
        assert deduplicate_results([r]) == [r]

    def test_backfill_candidates_used(self):
        r1 = _make_result("memory A", BASE_EMB, salience=8.0)
        r2 = _make_result("memory A dup", _similar_embedding(BASE_EMB), salience=3.0)
        backfill = _make_result("backfill item", [0.0, 0.0, 1.0, 0.0, 0.0], salience=4.0)

        deduped = deduplicate_results(
            [r1, r2], threshold=0.85, backfill_candidates=[backfill]
        )

        assert len(deduped) == 2
        contents = [r["content"] for r in deduped]
        assert "memory A" in contents
        assert "backfill item" in contents

    def test_backfill_skips_duplicates_of_kept(self):
        r1 = _make_result("memory A", BASE_EMB, salience=8.0)
        r2 = _make_result("memory A dup", _similar_embedding(BASE_EMB), salience=3.0)
        # Backfill candidate is also a near-duplicate of r1
        dup_backfill = _make_result("also dup", _similar_embedding(BASE_EMB, noise=0.005), salience=4.0)

        deduped = deduplicate_results(
            [r1, r2], threshold=0.85, backfill_candidates=[dup_backfill]
        )

        # Should NOT backfill because candidate is too similar to kept result
        assert len(deduped) == 1

    def test_none_embeddings_not_considered_duplicates(self):
        r1 = _make_result("no embed 1", None, salience=5.0)
        r2 = _make_result("no embed 2", None, salience=5.0)
        deduped = deduplicate_results([r1, r2], threshold=0.85)
        assert len(deduped) == 2


# ===========================================================================
# Three-pool retrieval (Task 8)
# ===========================================================================


# ---------------------------------------------------------------------------
# Creation freshness
# ---------------------------------------------------------------------------


class TestCreationFreshness:
    def test_brand_new_memory(self):
        now = datetime.now(timezone.utc)
        assert _creation_freshness(now) == pytest.approx(1.0, abs=0.01)

    def test_old_memory_decays(self):
        from datetime import timedelta

        old = datetime.now(timezone.utc) - timedelta(days=100)
        freshness = _creation_freshness(old)
        assert 0.0 < freshness < 0.5  # exp(-0.02 * 100) ≈ 0.135

    def test_naive_datetime_handled(self):
        """Naive datetimes treated as UTC."""
        now = datetime.utcnow()
        assert _creation_freshness(now) == pytest.approx(1.0, abs=0.02)


# ---------------------------------------------------------------------------
# RetrievalConfig defaults
# ---------------------------------------------------------------------------


class TestRetrievalConfig:
    def test_default_ratios_sum_to_one(self):
        cfg = RetrievalConfig()
        assert cfg.exploit_ratio + cfg.explore_ratio + cfg.narrative_ratio == pytest.approx(1.0)

    def test_custom_config(self):
        cfg = RetrievalConfig(total_results=10, exploit_ratio=0.5, explore_ratio=0.3, narrative_ratio=0.2)
        assert cfg.total_results == 10
        assert cfg.exploit_ratio == 0.5
        assert cfg.service_retrieval is False

    def test_pool_visibility_context_requires_user_or_service(self):
        retriever = PoolRetriever(RetrievalConfig())
        with pytest.raises(ValueError, match="requires user_id"):
            retriever._memory_visibility_context()

    def test_pool_visibility_context_allows_explicit_service_retrieval(self):
        retriever = PoolRetriever(RetrievalConfig(service_retrieval=True))
        context = retriever._memory_visibility_context()
        assert context.allow_global is True
        assert context.user_id == "system"


# ---------------------------------------------------------------------------
# Slot allocation
# ---------------------------------------------------------------------------


class TestAllocateSlots:
    def test_default_five_slots(self):
        retriever = PoolRetriever()
        slots = retriever._allocate_slots({
            PoolName.EXPLOIT: 0.60,
            PoolName.EXPLORE: 0.25,
            PoolName.NARRATIVE: 0.15,
        })

        assert sum(slots.values()) == 5
        assert all(v >= 1 for v in slots.values())
        # Exploit should get the most slots
        assert slots[PoolName.EXPLOIT] >= slots[PoolName.EXPLORE]
        assert slots[PoolName.EXPLOIT] >= slots[PoolName.NARRATIVE]

    def test_ten_slots(self):
        cfg = RetrievalConfig(total_results=10)
        retriever = PoolRetriever(cfg)
        slots = retriever._allocate_slots({
            PoolName.EXPLOIT: 0.60,
            PoolName.EXPLORE: 0.25,
            PoolName.NARRATIVE: 0.15,
        })

        assert sum(slots.values()) == 10
        assert slots[PoolName.EXPLOIT] == 6  # 60% of 10
        assert slots[PoolName.EXPLORE] >= 2
        assert slots[PoolName.NARRATIVE] >= 1

    def test_minimum_one_per_pool(self):
        cfg = RetrievalConfig(total_results=3)
        retriever = PoolRetriever(cfg)
        slots = retriever._allocate_slots({
            PoolName.EXPLOIT: 0.90,
            PoolName.EXPLORE: 0.05,
            PoolName.NARRATIVE: 0.05,
        })

        assert all(v >= 1 for v in slots.values())


# ---------------------------------------------------------------------------
# Pool tagging & orchestration (mocked pool methods)
# ---------------------------------------------------------------------------


class TestPoolRetrieverOrchestration:
    """Test the retrieve() method's orchestration logic with mocked pools."""

    def _make_pool_result(self, content, embedding=None, salience=5.0):
        return {
            "content": content,
            "embedding": embedding or [0.0] * 5,
            "salience": salience,
        }

    async def test_results_tagged_with_pool(self):
        retriever = PoolRetriever(RetrievalConfig(total_results=6))

        exploit_results = [self._make_pool_result("exploit-1", [1, 0, 0, 0, 0])]
        explore_results = [self._make_pool_result("explore-1", [0, 1, 0, 0, 0])]
        narrative_results = [self._make_pool_result("narrative-1", [0, 0, 1, 0, 0])]

        with patch.object(retriever, "_exploit_pool", return_value=exploit_results), \
             patch.object(retriever, "_explore_pool", return_value=explore_results), \
             patch.object(retriever, "_narrative_pool", return_value=narrative_results):

            results = await retriever.retrieve([0.5, 0.5, 0.5, 0.5, 0.5])

        pools_found = {r["_pool"] for r in results}
        assert PoolName.EXPLOIT in pools_found
        assert PoolName.EXPLORE in pools_found
        assert PoolName.NARRATIVE in pools_found

    async def test_dedup_across_pools(self):
        """Duplicate results across pools are deduplicated."""
        retriever = PoolRetriever(RetrievalConfig(total_results=5))

        shared_emb = [0.5, 0.3, 0.8, 0.1, 0.6]
        exploit_results = [self._make_pool_result("same memory", shared_emb, salience=8.0)]
        explore_results = [self._make_pool_result("same memory dup", _similar_embedding(shared_emb), salience=3.0)]
        narrative_results = [self._make_pool_result("narrative-1", [0, 0, 1, 0, 0])]

        with patch.object(retriever, "_exploit_pool", return_value=exploit_results), \
             patch.object(retriever, "_explore_pool", return_value=explore_results), \
             patch.object(retriever, "_narrative_pool", return_value=narrative_results):

            results = await retriever.retrieve([0.5, 0.5, 0.5, 0.5, 0.5])

        # The duplicate should be removed
        contents = [r["content"] for r in results]
        assert "same memory" in contents
        assert "same memory dup" not in contents

    async def test_respects_total_results_limit(self):
        retriever = PoolRetriever(RetrievalConfig(total_results=2))

        exploit_results = [
            self._make_pool_result("e1", [1, 0, 0, 0, 0]),
            self._make_pool_result("e2", [0.9, 0.1, 0, 0, 0]),
        ]
        explore_results = [self._make_pool_result("x1", [0, 1, 0, 0, 0])]
        narrative_results = [self._make_pool_result("n1", [0, 0, 1, 0, 0])]

        with patch.object(retriever, "_exploit_pool", return_value=exploit_results), \
             patch.object(retriever, "_explore_pool", return_value=explore_results), \
             patch.object(retriever, "_narrative_pool", return_value=narrative_results):

            results = await retriever.retrieve([0.5, 0.5, 0.5, 0.5, 0.5])

        assert len(results) <= 2

    async def test_custom_ratios_override(self):
        retriever = PoolRetriever(RetrievalConfig(total_results=6))

        with patch.object(retriever, "_exploit_pool", return_value=[]) as mock_exploit, \
             patch.object(retriever, "_explore_pool", return_value=[]) as mock_explore, \
             patch.object(retriever, "_narrative_pool", return_value=[]) as mock_narrative:

            await retriever.retrieve(
                [0.5, 0.5],
                ratios={PoolName.EXPLOIT: 0.5, PoolName.EXPLORE: 0.3, PoolName.NARRATIVE: 0.2},
            )

        # All three pools should have been called
        mock_exploit.assert_called_once()
        mock_explore.assert_called_once()
        mock_narrative.assert_called_once()

    async def test_empty_pools_return_empty(self):
        retriever = PoolRetriever()

        with patch.object(retriever, "_exploit_pool", return_value=[]), \
             patch.object(retriever, "_explore_pool", return_value=[]), \
             patch.object(retriever, "_narrative_pool", return_value=[]):

            results = await retriever.retrieve([0.5, 0.5])

        assert results == []

    async def test_no_session_pools_return_empty(self):
        """Without a session, pool methods return empty lists."""
        retriever = PoolRetriever()
        assert await retriever._exploit_pool([0.5], 3) == []
        assert await retriever._explore_pool([0.5], 3) == []
        assert await retriever._narrative_pool([0.5], 3) == []

    async def test_narrative_pool_suppresses_cross_org_rows(self):
        retriever = PoolRetriever(RetrievalConfig(user_id="user-1", org_id="org-1"))
        session = MagicMock()
        same_org = SimpleNamespace(
            id=1,
            arc_summary="same org narrative",
            title="same",
            topic_slug="same",
            semantic_embedding=[1.0, 0.0],
            user_id="user-2",
            org_id="org-1",
        )
        other_org = SimpleNamespace(
            id=2,
            arc_summary="other org narrative",
            title="other",
            topic_slug="other",
            semantic_embedding=[0.0, 1.0],
            user_id="user-3",
            org_id="org-2",
        )
        session.scalars = AsyncMock(return_value=MagicMock(all=MagicMock(return_value=[same_org, other_org])))

        results = await retriever._narrative_pool([0.5, 0.5], 3, session=session)

        assert [row["id"] for row in results] == [1]
        assert results[0]["visibility"] == "org"
        assert results[0]["org_id"] == "org-1"

    def test_memory_dict_records_tenant_scope_for_attention(self):
        memory = SimpleNamespace(
            id=7,
            content="tenant-scoped",
            memory_type="lesson",
            salience=8.0,
            semantic_embedding=[0.1, 0.2],
            access_count=0,
            created_at=datetime.now(timezone.utc),
            visibility="private",
            user_id="user-1",
            org_id="org-1",
        )

        result = PoolRetriever._memory_to_dict(memory)

        assert result["candidate_kind"] == "memory"
        assert result["user_id"] == "user-1"
        assert result["org_id"] == "org-1"
        assert result["visibility"] == "private"
