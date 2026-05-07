"""Tests for services/change_confidence.py — confidence-based routing."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))

from brain.systems.quality.confidence import (
    assess_confidence,
    score_scope,
    score_familiarity,
    score_test_coverage,
    score_reversibility,
    score_risk_surface,
    AUTO_MERGE_THRESHOLD,
)


class TestScoreScope:
    def test_single_file_few_lines(self):
        score, _ = score_scope(["config.json"], 5)
        assert score >= 0.9

    def test_many_files_many_lines(self):
        files = [f"file{i}.py" for i in range(12)]
        score, _ = score_scope(files, 600)
        assert score <= 0.3

    def test_moderate_change(self):
        score, _ = score_scope(["a.py", "b.py"], 40)
        assert 0.7 <= score <= 0.95

    def test_no_files(self):
        score, _ = score_scope([], 0)
        assert score == 1.0


class TestScoreFamiliarity:
    def test_no_brain_fn(self):
        score, _ = score_familiarity(["a.py"])
        assert score == 0.5

    def test_with_results(self):
        def brain_fn(q):
            return [{"content": "past change"}] * 5
        score, _ = score_familiarity(["a.py"], brain_fn)
        assert score >= 0.8

    def test_no_results(self):
        score, _ = score_familiarity(["a.py"], lambda q: [])
        assert score <= 0.4

    def test_brain_error(self):
        def bad_fn(q):
            raise RuntimeError("db down")
        score, _ = score_familiarity(["a.py"], bad_fn)
        assert score == 0.4


class TestScoreTestCoverage:
    def test_all_passing_with_new_tests(self):
        score, _ = score_test_coverage(True, True, True)
        assert score >= 0.9

    def test_passing_no_new_tests(self):
        score, _ = score_test_coverage(True, True, False)
        assert 0.5 <= score <= 0.8

    def test_no_tests_exist(self):
        score, _ = score_test_coverage(True, False, False)
        assert score <= 0.3

    def test_tests_failing(self):
        score, _ = score_test_coverage(False, True, True)
        assert score <= 0.2


class TestScoreReversibility:
    def test_new_file(self):
        score, _ = score_reversibility(["new_module.py"], is_new_file=True)
        assert score >= 0.8

    def test_config_file(self):
        score, _ = score_reversibility(["settings.json"])
        assert score >= 0.8

    def test_migration_file(self):
        score, _ = score_reversibility(["migrations/001_add_table.py"])
        assert score <= 0.3

    def test_mixed_files(self):
        score, _ = score_reversibility(["config.yaml", "core/db.py"])
        assert 0.5 <= score <= 0.85


class TestScoreRiskSurface:
    def test_no_risk(self):
        score, _ = score_risk_surface(["utils.py"], "def helper(): pass")
        assert score >= 0.8

    def test_auth_risk(self):
        score, _ = score_risk_surface(["auth/login.py"], "check token validity")
        assert score <= 0.7

    def test_multiple_risks(self):
        score, _ = score_risk_surface(
            ["api/v2/endpoint.py", "migrations/002.py"],
            "ALTER TABLE users; auth token check; requests.post(webhook)"
        )
        assert score <= 0.4


class TestAssessConfidence:
    def test_simple_config_change_auto_merges(self):
        result = assess_confidence(
            files_changed=["config.json"],
            lines_changed=3,
            tests_passed=True,
            tests_exist=True,
            identified_risks=["Config could break if schema changes"],
        )
        assert result["route"] == "auto_merge"
        assert result["confidence"] >= AUTO_MERGE_THRESHOLD

    def test_multi_file_schema_change_needs_review(self):
        result = assess_confidence(
            files_changed=["migrations/001.py", "models/user.py", "api/v2.py",
                          "tests/test_user.py", "schema.sql"],
            lines_changed=300,
            diff_content="ALTER TABLE users ADD COLUMN; auth token; requests.post",
            tests_passed=True,
            tests_exist=True,
            identified_risks=["Schema migration is irreversible", "API change could break clients"],
        )
        assert result["route"] == "pr_for_review"
        assert result["confidence"] < AUTO_MERGE_THRESHOLD

    def test_failing_tests_always_review(self):
        result = assess_confidence(
            files_changed=["simple.py"],
            lines_changed=2,
            tests_passed=False,
            tests_exist=True,
        )
        assert result["route"] == "pr_for_review"

    def test_self_honesty_penalty(self):
        """When no risks are identified, confidence should be reduced."""
        result_with_risks = assess_confidence(
            files_changed=["a.py"],
            lines_changed=5,
            tests_passed=True,
            tests_exist=True,
            identified_risks=["Could break imports"],
        )
        result_no_risks = assess_confidence(
            files_changed=["a.py"],
            lines_changed=5,
            tests_passed=True,
            tests_exist=True,
            identified_risks=[],  # Empty = penalty
        )
        assert result_no_risks["confidence"] < result_with_risks["confidence"]
        assert "honesty penalty" in result_no_risks["reasoning"].lower()

    def test_reasoning_includes_what_could_go_wrong(self):
        result = assess_confidence(
            files_changed=["core/db.py"],
            lines_changed=50,
            tests_passed=True,
            tests_exist=True,
        )
        assert "What could go wrong" in result["reasoning"]

    def test_new_file_high_confidence(self):
        result = assess_confidence(
            files_changed=["services/new_feature.py"],
            lines_changed=30,
            tests_passed=True,
            tests_exist=True,
            new_tests_written=True,
            is_new_file=True,
            identified_risks=["New module may have edge cases"],
        )
        assert result["confidence"] >= 0.6

    def test_factors_present(self):
        result = assess_confidence(
            files_changed=["a.py"], lines_changed=5,
            tests_passed=True, tests_exist=True,
        )
        assert set(result["factors"].keys()) == {
            "scope", "familiarity", "test_coverage", "reversibility", "risk_surface"
        }
