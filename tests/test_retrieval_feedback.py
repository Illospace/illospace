#!/usr/bin/env python3
"""Tests for retrieval feedback loop (Issue #5).

Uses rollback_cursor fixture — all writes are rolled back after each test.
Zero test data leaks to production DB.
"""

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))

import pytest
from unittest.mock import MagicMock, patch
from sqlalchemy import text

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DB_URL"),
    reason="TEST_DB_URL not set — run via scripts/test-with-db.sh",
)

from brain.systems.memory.retrieval_feedback import (
    analyze_missed_memories,
    apply_retrieval_feedback,
    record_attention_usefulness,
)

ORG_ID = "30000000-0000-0000-0000-000000000001"
USER_ID = "40000000-0000-0000-0000-000000000001"


@pytest.fixture
def scoped_principal(db_session):
    db_session.execute(text("""
        INSERT INTO orgs (id, name, slug)
        VALUES (:org_id, 'Retrieval Feedback Test', 'retrieval-feedback-test')
        ON CONFLICT (id) DO NOTHING
    """), {"org_id": ORG_ID})
    db_session.execute(text("""
        INSERT INTO users (id, org_id, name, email, role, approved)
        VALUES (:user_id, :org_id, 'Retrieval Feedback Test', 'retrieval-feedback@example.com', 'owner', TRUE)
        ON CONFLICT (id) DO NOTHING
    """), {"user_id": USER_ID, "org_id": ORG_ID})
    db_session.flush()
    return {"user_id": USER_ID, "org_id": ORG_ID}


def _ensure_test_memory(db_session, content="test memory", salience=5.0):
    """Create a test memory and return its id."""
    row = db_session.execute(text("""
        INSERT INTO memories (
            content, memory_type, salience, source, tags,
            user_id, org_id, visibility
        )
        VALUES (
            :content, 'fact', :salience, 'test', ARRAY[]::text[],
            :user_id, :org_id, 'private'
        )
        RETURNING id
    """), {
        "content": content,
        "salience": salience,
        "user_id": USER_ID,
        "org_id": ORG_ID,
    }).mappings().first()
    return row["id"]


def _ensure_retrieval_log(db_session, memory_id, query="test query"):
    """Create a retrieval_log entry pointing to memory_id, return log id."""
    row = db_session.execute(text("""
        INSERT INTO retrieval_log (query_text, results_returned, top_result_id, top_score, org_id)
        VALUES (:query, 1, :memory_id, 0.85, :org_id) RETURNING id
    """), {"query": query, "memory_id": memory_id, "org_id": ORG_ID}).mappings().first()
    return row["id"]


class TestApplyRetrievalFeedback:
    def test_hit_boosts_salience(self, db_session, scoped_principal, unit_of_work_for_session):
        mid = _ensure_test_memory(db_session, "hit test", salience=5.0)
        log_id = _ensure_retrieval_log(db_session, mid)
        with patch("brain.systems.memory.retrieval_feedback.UnitOfWork", unit_of_work_for_session):
            apply_retrieval_feedback(log_id, "hit")
        row = db_session.execute(text("SELECT salience FROM memories WHERE id = :id"), {"id": mid}).mappings().first()
        assert row["salience"] == 5.5

    def test_hit_caps_at_10(self, db_session, scoped_principal, unit_of_work_for_session):
        mid = _ensure_test_memory(db_session, "cap test", salience=9.8)
        log_id = _ensure_retrieval_log(db_session, mid)
        with patch("brain.systems.memory.retrieval_feedback.UnitOfWork", unit_of_work_for_session):
            apply_retrieval_feedback(log_id, "hit")
        row = db_session.execute(text("SELECT salience FROM memories WHERE id = :id"), {"id": mid}).mappings().first()
        assert row["salience"] == 10.0

    def test_miss_decreases_salience(self, db_session, scoped_principal, unit_of_work_for_session):
        mid = _ensure_test_memory(db_session, "miss test", salience=5.0)
        log_id = _ensure_retrieval_log(db_session, mid)
        with patch("brain.systems.memory.retrieval_feedback.UnitOfWork", unit_of_work_for_session):
            apply_retrieval_feedback(log_id, "miss")
        row = db_session.execute(text("SELECT salience FROM memories WHERE id = :id"), {"id": mid}).mappings().first()
        assert abs(row["salience"] - 4.7) < 0.01

    def test_miss_floors_at_1(self, db_session, scoped_principal, unit_of_work_for_session):
        mid = _ensure_test_memory(db_session, "floor test", salience=1.1)
        log_id = _ensure_retrieval_log(db_session, mid)
        with patch("brain.systems.memory.retrieval_feedback.UnitOfWork", unit_of_work_for_session):
            apply_retrieval_feedback(log_id, "miss")
        row = db_session.execute(text("SELECT salience FROM memories WHERE id = :id"), {"id": mid}).mappings().first()
        assert row["salience"] == 1.0

    def test_partial_no_salience_change(self, db_session, scoped_principal, unit_of_work_for_session):
        mid = _ensure_test_memory(db_session, "partial test", salience=5.0)
        log_id = _ensure_retrieval_log(db_session, mid)
        with patch("brain.systems.memory.retrieval_feedback.UnitOfWork", unit_of_work_for_session):
            apply_retrieval_feedback(log_id, "partial")
        row = db_session.execute(text("SELECT salience FROM memories WHERE id = :id"), {"id": mid}).mappings().first()
        assert row["salience"] == 5.0

    def test_feedback_stored_in_log(self, db_session, scoped_principal, unit_of_work_for_session):
        mid = _ensure_test_memory(db_session, "log test", salience=5.0)
        log_id = _ensure_retrieval_log(db_session, mid)
        with patch("brain.systems.memory.retrieval_feedback.UnitOfWork", unit_of_work_for_session):
            apply_retrieval_feedback(log_id, "hit")
        row = db_session.execute(
            text("SELECT was_relevant, feedback FROM retrieval_log WHERE id = :id"),
            {"id": log_id},
        ).mappings().first()
        assert row["was_relevant"] is True
        assert row["feedback"] == "hit"

    def test_no_top_result_id_graceful(self, db_session, scoped_principal, unit_of_work_for_session):
        """If retrieval_log has no top_result_id, feedback is logged but no salience change."""
        row = db_session.execute(text("""
            INSERT INTO retrieval_log (query_text, results_returned, top_result_id, top_score, org_id)
            VALUES ('no result query', 0, NULL, NULL, :org_id) RETURNING id
        """), {"org_id": ORG_ID}).mappings().first()
        log_id = row["id"]
        with patch("brain.systems.memory.retrieval_feedback.UnitOfWork", unit_of_work_for_session):
            apply_retrieval_feedback(log_id, "miss")
        row = db_session.execute(
            text("SELECT feedback FROM retrieval_log WHERE id = :id"),
            {"id": log_id},
        ).mappings().first()
        assert row["feedback"] == "miss"


class TestAnalyzeMissedMemories:
    def test_identifies_consistently_missed(self, db_session, scoped_principal, unit_of_work_for_session):
        mid = _ensure_test_memory(db_session, "always missed", salience=5.0)
        for _ in range(4):
            log_id = _ensure_retrieval_log(db_session, mid, "missed query")
            with patch("brain.systems.memory.retrieval_feedback.UnitOfWork", unit_of_work_for_session):
                apply_retrieval_feedback(log_id, "miss")

        with patch("brain.systems.memory.retrieval_feedback.UnitOfWork", unit_of_work_for_session):
            missed = analyze_missed_memories(min_misses=3, days=30)
        missed_ids = [m["memory_id"] for m in missed]
        assert mid in missed_ids

    def test_hit_memories_not_flagged(self, db_session, scoped_principal, unit_of_work_for_session):
        mid = _ensure_test_memory(db_session, "always hit", salience=5.0)
        for _ in range(4):
            log_id = _ensure_retrieval_log(db_session, mid, "hit query")
            with patch("brain.systems.memory.retrieval_feedback.UnitOfWork", unit_of_work_for_session):
                apply_retrieval_feedback(log_id, "hit")

        with patch("brain.systems.memory.retrieval_feedback.UnitOfWork", unit_of_work_for_session):
            missed = analyze_missed_memories(min_misses=3, days=30)
        missed_ids = [m["memory_id"] for m in missed]
        assert mid not in missed_ids


class TestAttentionUsefulnessAttribution:
    def test_records_real_signals_on_feedback_row(self, rollback_cursor):
        from brain.platform.db.models.system import RetrievalItemFeedback

        feedback_row = RetrievalItemFeedback(
            retrieval_decision_id=71,
            memory_id=33,
            candidate_source="memory",
            user_id="user-1",
            org_id="org-1",
        )

        mock_uow = MagicMock()
        mock_uow.__enter__ = MagicMock(return_value=mock_uow)
        mock_uow.__exit__ = MagicMock(return_value=False)
        mock_uow.session.scalars.return_value.first.return_value = feedback_row

        with patch("brain.systems.memory.attention_controller.UnitOfWork", return_value=mock_uow):
            ok = record_attention_usefulness(
                71,
                user_id="user-1",
                org_id="org-1",
                item_id=33,
                actually_used=True,
                cited_in_output=True,
                correlated_with_success=True,
                lazy_loaded=True,
                retry_delta=-2,
                verifier_helped=True,
                user_feedback_signal="helpful",
            )

        assert ok is True
        assert feedback_row.actually_used is True
        assert feedback_row.cited_in_output is True
        assert feedback_row.correlated_with_success is True
        assert feedback_row.lazy_loaded is True
        assert feedback_row.retry_delta == -2
        assert feedback_row.verifier_helped is True
        assert feedback_row.user_feedback_signal == "helpful"
