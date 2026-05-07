"""Tests for token optimization in cortex run.

After the ORM migration, several functions (_build_skill_context,
_parse_jsonl_usage, run_metrics, backfill_tokens) were removed.
The remaining tests cover budget estimation and message size behavior
that still exist in the run module.
"""

import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ============================================================
# Budget estimation tests
# ============================================================

class TestBudgetEstimation:
    """Token budget estimation exists and is callable."""

    def test_estimate_function_exists(self):
        from brain.systems.budget import estimate_run_tokens
        assert callable(estimate_run_tokens)

    def test_estimate_returns_positive(self):
        from brain.systems.budget import estimate_run_tokens
        result = estimate_run_tokens("Fix the bug in the login form")
        assert isinstance(result, (int, float))
        assert result > 0

    def test_short_message_estimates_less(self):
        from brain.systems.budget import estimate_run_tokens
        short = estimate_run_tokens("ok")
        long = estimate_run_tokens(
            "Implement a full OAuth2 login flow with PKCE, refresh tokens, "
            "session management, CSRF protection, and rate limiting"
        )
        assert short <= long


# ============================================================
# Thread summary budget constants
# ============================================================

class TestThreadSummaryConstants:
    """Thread summary budget constants exist."""

    def test_max_thread_summary_chars_exists(self):
        from brain.systems.budget import MAX_THREAD_SUMMARY_CHARS
        assert MAX_THREAD_SUMMARY_CHARS > 0

    def test_max_last_messages_exists(self):
        from brain.systems.budget import MAX_LAST_MESSAGES
        assert MAX_LAST_MESSAGES > 0


# ============================================================
# Run queue uses ORM
# ============================================================

class TestRunUsesORM:
    """Run submodules use UnitOfWork (not db_module)."""

    def test_run_submodules_use_unit_of_work(self):
        """At least one run submodule should import UnitOfWork."""
        import brain.systems.runs.cortex.runner as q
        # UnitOfWork is used within the submodules (imported locally or at top-level)
        assert hasattr(q, 'UnitOfWork')

    def test_run_no_db_module(self):
        import brain.systems.runs.cortex as cd
        assert not hasattr(cd, 'db_module'), "run should use UnitOfWork, not db_module"
