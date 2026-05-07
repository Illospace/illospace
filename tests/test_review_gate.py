"""Tests for child agent output review gate."""

from unittest.mock import patch

# Will be implemented in services/review_gate.py
from brain.systems.quality.review import review_output, ReviewResult


class TestReviewResult:
    def test_result_has_required_fields(self):
        r = ReviewResult(passed=True, concerns=[], score=0.9)
        assert r.passed is True
        assert r.concerns == []
        assert r.score == 0.9

    def test_result_failed(self):
        r = ReviewResult(passed=False, concerns=["bad code"], score=0.3)
        assert r.passed is False
        assert len(r.concerns) == 1


class TestFlagsPhantomTests:
    """Flags output that claims 'all tests pass' without test files existing."""

    def test_flags_claimed_tests_no_test_file(self, mock_db, mock_embeddings):
        result = review_output(
            task="Add validation to user model",
            output="Implemented validation. All tests pass. ✅",
            files_changed=["models/user.py"],
        )
        assert not result.passed
        assert any("test" in c.lower() for c in result.concerns)

    def test_passes_when_test_file_included(self, mock_db, mock_embeddings):
        result = review_output(
            task="Add validation to user model",
            output="Implemented validation. All tests pass. ✅",
            files_changed=["models/user.py", "tests/test_user.py"],
        )
        # Should not flag phantom tests concern
        assert not any("phantom" in c.lower() or "no test file" in c.lower() for c in result.concerns)

    def test_flags_tests_pass_variations(self, mock_db, mock_embeddings):
        for phrase in ["tests pass", "tests passing", "test suite passes", "all tests green"]:
            result = review_output(
                task="Fix bug",
                output=f"Fixed. {phrase}.",
                files_changed=["fix.py"],
            )
            assert any("test" in c.lower() for c in result.concerns), f"Failed to flag: {phrase}"


class TestTaskOutputMismatch:
    """Warns when task asked for investigation but output is implementation."""

    def test_flags_implementation_when_investigation_requested(self, mock_db, mock_embeddings):
        result = review_output(
            task="Investigate why the API returns 500 errors",
            output="Fixed the bug by adding a try/except. All working now.",
            files_changed=["api/handler.py"],
        )
        assert not result.passed
        assert any("investigate" in c.lower() or "mismatch" in c.lower() for c in result.concerns)

    def test_passes_investigation_output_for_investigation_task(self, mock_db, mock_embeddings):
        result = review_output(
            task="Investigate why the API returns 500 errors",
            output="Root cause: the DB connection pool exhausts under load. "
                   "Found 3 endpoints that don't release connections. "
                   "Recommendation: switch to context manager pattern.",
            files_changed=[],
        )
        assert not any("mismatch" in c.lower() for c in result.concerns)

    def test_passes_implementation_for_implementation_task(self, mock_db, mock_embeddings):
        result = review_output(
            task="Fix the database connection leak",
            output="Refactored all 3 endpoints to use context managers. Tests added and passing.",
            files_changed=["api/handler.py", "tests/test_handler.py"],
        )
        assert not any("mismatch" in c.lower() for c in result.concerns)


class TestCleanOutput:
    """Passes clean output that matches requirements."""

    def test_passes_well_formed_output(self, mock_db, mock_embeddings):
        result = review_output(
            task="Add retry logic to the webhook handler",
            output="Added exponential backoff retry with max 3 attempts. "
                   "Tests cover: success on first try, success on retry, "
                   "failure after max retries. All 3 tests pass.",
            files_changed=["services/webhook.py", "tests/test_webhook.py"],
        )
        assert result.passed
        assert result.score >= 0.7

    def test_passes_investigation_with_findings(self, mock_db, mock_embeddings):
        result = review_output(
            task="Investigate memory leak in worker process",
            output="Found the leak: event listeners not cleaned up on disconnect. "
                   "3 instances in websocket.py lines 45, 89, 132. "
                   "Each creates a closure that holds a reference to the connection object.",
            files_changed=[],
        )
        assert result.passed


class TestBrainQueryIntegration:
    """Queries brain for relevant past failures."""

    def test_queries_brain_for_context(self, mock_db, mock_embeddings):
        """Verify the review gate queries the brain for relevant context."""
        with patch("brain.systems.quality.review.get_context") as mock_ctx:
            mock_ctx.return_value = {
                "memories": [],
                "guardrails": [{"skill": "develop", "failure": "Shipped without tests", "when": "2026-03-01"}],
                "warnings": ["Always verify test output exists"],
            }
            result = review_output(
                task="Build new feature",
                output="Feature complete. Tests pass.",
                files_changed=["feature.py"],
            )
            mock_ctx.assert_called_once()
            # Should incorporate guardrail warnings
            assert any("test" in c.lower() for c in result.concerns)

    def test_works_when_brain_unavailable(self, mock_db, mock_embeddings):
        """Review gate should still work if brain query fails."""
        with patch("brain.systems.quality.review.get_context", side_effect=Exception("DB down")):
            result = review_output(
                task="Fix bug",
                output="Fixed by adding null check. Tests in test_fix.py pass.",
                files_changed=["fix.py", "tests/test_fix.py"],
            )
            # Should still produce a result, just without brain context
            assert isinstance(result, ReviewResult)


class TestDRYCheck:
    """Flags potential DRY violations."""

    def test_flags_too_many_files_changed(self, mock_db, mock_embeddings):
        result = review_output(
            task="Add logging",
            output="Added logging to all handlers.",
            files_changed=[f"handlers/handler_{i}.py" for i in range(15)],
        )
        assert any("dry" in c.lower() or "files" in c.lower() for c in result.concerns)


class TestScoring:
    """Review gate produces meaningful scores."""

    def test_low_score_for_bad_output(self, mock_db, mock_embeddings):
        result = review_output(
            task="Investigate the performance issue",
            output="Fixed it.",
            files_changed=["perf.py"],
        )
        assert result.score < 0.5

    def test_high_score_for_good_output(self, mock_db, mock_embeddings):
        result = review_output(
            task="Add input validation",
            output="Added validation for email, phone, and name fields. "
                   "Each validates format and length. Tests cover valid input, "
                   "invalid format, empty strings, and boundary lengths.",
            files_changed=["models/user.py", "tests/test_user_validation.py"],
        )
        assert result.score >= 0.7
