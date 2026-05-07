"""Tests for brain.systems.memory.integrity — nightly memory integrity checks."""
from datetime import datetime, timezone

import pytest

from brain.systems.memory.integrity import (
    IntegrityResult,
    check_dag_consistency,
    check_duplicates,
    check_embedding_staleness,
    check_orphan_memories,
)


# ---------------------------------------------------------------------------
# check_orphan_memories
# ---------------------------------------------------------------------------


class TestCheckOrphanMemories:
    def test_flags_isolated_low_salience(self):
        memories = [
            {"id": 1, "edge_count": 0, "access_count": 0, "salience": 1.5},
            {"id": 2, "edge_count": 0, "access_count": 1, "salience": 1.0},
        ]
        result = check_orphan_memories(memories)
        assert result.status == "warning"
        assert result.check_type == "orphan_memories"
        assert set(result.details["orphan_ids"]) == {1, 2}

    def test_passes_when_all_connected(self):
        memories = [
            {"id": 1, "edge_count": 3, "access_count": 5, "salience": 7.0},
            {"id": 2, "edge_count": 1, "access_count": 2, "salience": 4.0},
        ]
        result = check_orphan_memories(memories)
        assert result.status == "passed"

    def test_respects_salience_threshold(self):
        memories = [
            {"id": 1, "edge_count": 0, "access_count": 0, "salience": 3.0},
        ]
        # Default threshold is 2.0, so salience 3.0 is above it
        result = check_orphan_memories(memories)
        assert result.status == "passed"

    def test_custom_threshold(self):
        memories = [
            {"id": 1, "edge_count": 0, "access_count": 0, "salience": 3.0},
        ]
        result = check_orphan_memories(memories, salience_threshold=5.0)
        assert result.status == "warning"
        assert result.details["orphan_ids"] == [1]

    def test_empty_input(self):
        result = check_orphan_memories([])
        assert result.status == "passed"

    def test_access_count_above_1_not_orphan(self):
        memories = [
            {"id": 1, "edge_count": 0, "access_count": 2, "salience": 1.0},
        ]
        result = check_orphan_memories(memories)
        assert result.status == "passed"


# ---------------------------------------------------------------------------
# check_embedding_staleness
# ---------------------------------------------------------------------------


class TestCheckEmbeddingStaleness:
    def test_finds_stale_embeddings(self):
        t1 = datetime(2026, 3, 10, tzinfo=timezone.utc)
        t2 = datetime(2026, 3, 15, tzinfo=timezone.utc)
        memories = [
            {"id": 1, "updated_at": t2, "embedded_at": t1},
            {"id": 2, "updated_at": t1, "embedded_at": t2},
        ]
        result = check_embedding_staleness(memories)
        assert result.status == "warning"
        assert result.details["stale_ids"] == [1]

    def test_passes_when_fresh(self):
        t = datetime(2026, 3, 15, tzinfo=timezone.utc)
        memories = [
            {"id": 1, "updated_at": t, "embedded_at": t},
        ]
        result = check_embedding_staleness(memories)
        assert result.status == "passed"

    def test_skips_null_timestamps(self):
        memories = [
            {"id": 1, "updated_at": None, "embedded_at": None},
        ]
        result = check_embedding_staleness(memories)
        assert result.status == "passed"


# ---------------------------------------------------------------------------
# check_dag_consistency
# ---------------------------------------------------------------------------


class TestCheckDagConsistency:
    def test_detects_missing_children(self):
        summaries = [
            {"child_ids": [1, 2, 3]},
            {"child_ids": [4, 5]},
        ]
        existing = {1, 2, 4}
        result = check_dag_consistency(summaries, existing)
        assert result.status == "warning"
        assert set(result.details["missing_child_ids"]) == {3, 5}

    def test_passes_when_all_exist(self):
        summaries = [
            {"child_ids": [1, 2]},
            {"child_ids": [3]},
        ]
        existing = {1, 2, 3}
        result = check_dag_consistency(summaries, existing)
        assert result.status == "passed"

    def test_empty_summaries(self):
        result = check_dag_consistency([], set())
        assert result.status == "passed"


# ---------------------------------------------------------------------------
# check_duplicates
# ---------------------------------------------------------------------------


class TestCheckDuplicates:
    def test_flags_high_similarity_pairs(self):
        pairs = [
            (1, 2, 0.95),
            (3, 4, 0.80),
            (5, 6, 0.93),
        ]
        result = check_duplicates(pairs)
        assert result.status == "warning"
        assert result.details["count"] == 2
        assert (1, 2) in result.details["flagged_pairs"]
        assert (5, 6) in result.details["flagged_pairs"]

    def test_passes_when_below_threshold(self):
        pairs = [
            (1, 2, 0.50),
            (3, 4, 0.91),
        ]
        result = check_duplicates(pairs)
        assert result.status == "passed"

    def test_custom_threshold(self):
        pairs = [(1, 2, 0.85)]
        result = check_duplicates(pairs, threshold=0.80)
        assert result.status == "warning"

    def test_empty_pairs(self):
        result = check_duplicates([])
        assert result.status == "passed"


# ---------------------------------------------------------------------------
# IntegrityResult dataclass
# ---------------------------------------------------------------------------


class TestIntegrityResult:
    def test_defaults(self):
        r = IntegrityResult(check_type="test", status="passed")
        assert r.details == {}
        assert r.auto_repaired == 0

    def test_custom_fields(self):
        r = IntegrityResult(
            check_type="orphans",
            status="warning",
            details={"ids": [1, 2]},
            auto_repaired=2,
        )
        assert r.check_type == "orphans"
        assert r.auto_repaired == 2
