"""Tests for the co-evolutionary doer-critic system."""

import json
import os
import sys
from unittest.mock import MagicMock, patch, call

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeSession:
    """Tracks SQL executions and returns configurable results."""

    def __init__(self):
        self.queries = []
        self.params = []
        self._results = []

    def execute(self, sql, params=None):
        self.queries.append(str(sql).strip())
        self.params.append(params)
        result = MagicMock()
        if self._results:
            data = self._results.pop(0)
            # If it's a list, it's for .mappings().all(); if dict, for .mappings().first()
            if isinstance(data, list):
                result.mappings.return_value.all.return_value = data
                result.mappings.return_value.first.return_value = data[0] if data else None
            elif data is None:
                result.mappings.return_value.all.return_value = []
                result.mappings.return_value.first.return_value = None
            else:
                result.mappings.return_value.first.return_value = data
                result.mappings.return_value.all.return_value = [data]
        else:
            result.mappings.return_value.first.return_value = {"id": 1}
            result.mappings.return_value.all.return_value = []
        return result

    def queue_result(self, result):
        """Queue a result for the next execute call."""
        self._results.append(result)


@pytest.fixture
def fake_session():
    return FakeSession()


@pytest.fixture
def patched_db(fake_session):
    mock_uow = MagicMock()
    mock_uow.__enter__ = MagicMock(return_value=mock_uow)
    mock_uow.__exit__ = MagicMock(return_value=False)
    mock_uow.session = fake_session

    with patch("brain.app.cli.critic_system.UnitOfWork", return_value=mock_uow):
        yield fake_session


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestRecordCriticReview:
    def test_inserts_review(self, patched_db):
        import brain.app.cli.critic_system as critic_system
        rid = critic_system.record_critic_review(
            execution_id=42,
            findings=[{"issue": "no error handling", "severity": "high"}],
            scores={"quality": 6},
            critic_skill_id=5,
            verdict="revise",
        )
        assert rid == 1
        sql = patched_db.queries[-1]
        assert "INSERT INTO critic_reviews" in sql


class TestRecordOutcome:
    def test_invalid_outcome_raises(self, patched_db):
        import brain.app.cli.critic_system as critic_system
        with pytest.raises(ValueError, match="Invalid outcome"):
            critic_system.record_outcome(1, "unknown")

    def test_records_and_triggers_update(self, patched_db):
        import brain.app.cli.critic_system as critic_system

        # Queue: auto-find critic review, then INSERT returning id
        patched_db.queue_result({"id": 10})  # critic review lookup
        patched_db.queue_result({"id": 99})  # INSERT returning

        with patch.object(critic_system, "update_both_skills") as mock_update:
            oid = critic_system.record_outcome(42, "success", "production", "all good")
            mock_update.assert_called_once_with(42, "success", 10)


class TestUpdateBothSkills:
    """The core co-evolution logic."""

    def _run_update(self, patched_db, verdict, has_findings, outcome):
        import brain.app.cli.critic_system as critic_system

        findings = [{"issue": "test"}] if has_findings else []
        patched_db.queue_result({
            "id": 10, "critic_skill_id": 5,
            "findings": findings, "verdict": verdict,
            "execution_id": 42, "scores": {}, "created_at": None,
        })

        critic_system.update_both_skills(42, outcome, critic_review_id=10)
        return patched_db

    def test_outcome_updates_both_skills(self, patched_db):
        """True positive: critic flagged issue, outcome was failure -> critic improves."""
        session = self._run_update(patched_db, "reject", True, "failure")
        # Should have UPDATE with success_count + 1
        update_sql = [q for q in session.queries if "UPDATE skills" in q]
        assert len(update_sql) == 1
        assert "success_count = success_count + 1" in update_sql[0]

    def test_critic_false_positive_degrades_critic(self, patched_db):
        """Critic flagged issues but outcome was success -> critic degrades."""
        session = self._run_update(patched_db, "reject", True, "success")
        update_sql = [q for q in session.queries if "UPDATE skills" in q]
        assert len(update_sql) == 1
        assert "failure_count = failure_count + 1" in update_sql[0]

    def test_critic_miss_degrades_critic(self, patched_db):
        """Critic approved but outcome was failure -> critic missed it -> degrades."""
        session = self._run_update(patched_db, "approve", False, "failure")
        update_sql = [q for q in session.queries if "UPDATE skills" in q]
        assert len(update_sql) == 1
        assert "failure_count = failure_count + 1" in update_sql[0]

    def test_true_negative_improves_critic(self, patched_db):
        """Critic approved and outcome was success -> correct -> improves."""
        session = self._run_update(patched_db, "approve", False, "success")
        update_sql = [q for q in session.queries if "UPDATE skills" in q]
        assert len(update_sql) == 1
        assert "success_count = success_count + 1" in update_sql[0]

    def test_no_critic_review_noop(self, patched_db):
        """No critic review -> nothing to update."""
        import brain.app.cli.critic_system as critic_system
        critic_system.update_both_skills(42, "success", critic_review_id=None)
        update_sql = [q for q in patched_db.queries if "UPDATE" in q]
        assert len(update_sql) == 0


class TestGetCriticContext:
    def test_returns_empty_for_unknown_skill(self, patched_db):
        import brain.app.cli.critic_system as critic_system
        patched_db.queue_result(None)  # skill not found
        ctx = critic_system.get_critic_context("nonexistent")
        assert ctx["false_positives"] == []
        assert ctx["misses"] == []
        assert ctx["precision"] is None

    def test_context_includes_past_misses(self, patched_db):
        import brain.app.cli.critic_system as critic_system

        # Skill lookup
        patched_db.queue_result({"id": 5})
        # False positives query
        patched_db.queue_result([
            {"task_description": "deploy v2", "findings": [{"issue": "risky"}],
             "outcome": "success", "notes": None}
        ])
        # Misses query
        patched_db.queue_result([
            {"task_description": "migrate db", "notes": "crashed in prod", "outcome": "failure"}
        ])
        # Metrics query
        patched_db.queue_result({"tp": 3, "fp": 1, "fn": 1, "tn": 5})

        ctx = critic_system.get_critic_context("code-review")
        assert len(ctx["false_positives"]) == 1
        assert ctx["false_positives"][0]["task"] == "deploy v2"
        assert len(ctx["misses"]) == 1
        assert ctx["misses"][0]["task"] == "migrate db"
        assert ctx["precision"] == 0.75  # 3/(3+1)
        assert ctx["recall"] == 0.75     # 3/(3+1)


class TestAnalyzeCriticHealth:
    def test_returns_metrics_per_skill(self, patched_db):
        import brain.app.cli.critic_system as critic_system

        # critic skills query
        patched_db.queue_result([
            {"id": 5, "name": "code-review-critic"},
        ])
        # metrics for that skill
        patched_db.queue_result({"tp": 10, "fp": 2, "fn": 1, "tn": 7})

        health = critic_system.analyze_critic_health()
        assert len(health) == 1
        assert health[0]["skill_name"] == "code-review-critic"
        assert health[0]["precision"] == round(10 / 12, 3)
        assert health[0]["recall"] == round(10 / 11, 3)

    def test_empty_when_no_critics(self, patched_db):
        import brain.app.cli.critic_system as critic_system
        patched_db.queue_result([])  # no critic skills
        assert critic_system.analyze_critic_health() == []
