"""Tests for run.py — deterministic task run system."""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))


# ============================================================
# classify_task tests
# ============================================================

class TestClassifyTask:
    """Task classifier returns correct types for known patterns."""

    def test_edit_file_keywords(self):
        from brain.app.cli.run import classify_task
        assert classify_task("edit the config file") == "edit_file"
        assert classify_task("update file session_hooks.py") == "edit_file"
        assert classify_task("fix the file db.py") == "edit_file"
        assert classify_task("modify file config.md") == "edit_file"

    def test_investigate_keywords(self):
        from brain.app.cli.run import classify_task
        assert classify_task("investigate why the API is slow") == "investigate"
        assert classify_task("debug the login flow") == "investigate"
        assert classify_task("trace the error in production") == "investigate"
        assert classify_task("find out why tokens are leaking") == "investigate"
        assert classify_task("look into the memory leak") == "investigate"

    def test_implement_keywords(self):
        from brain.app.cli.run import classify_task
        assert classify_task("implement rate limiting") == "implement"
        assert classify_task("build the run system") == "implement"
        assert classify_task("create a new API endpoint") == "implement"
        assert classify_task("add authentication middleware") == "implement"
        assert classify_task("write a migration script") == "implement"

    def test_review_keywords(self):
        from brain.app.cli.run import classify_task
        assert classify_task("review the PR for auth changes") == "review"
        assert classify_task("check the migration script") == "review"
        assert classify_task("audit the security config") == "review"
        assert classify_task("verify the deploy pipeline") == "review"

    def test_encode_keywords(self):
        from brain.app.cli.run import classify_task
        assert classify_task("encode this lesson about TDD") == "encode"
        assert classify_task("remember that Alex prefers DRY") == "encode"
        assert classify_task("save memory about the API patterns") == "encode"

    def test_fallback_to_custom(self):
        from brain.app.cli.run import classify_task
        assert classify_task("do something vague") == "custom"
        assert classify_task("hello world") == "custom"

    def test_explicit_type_overrides(self):
        """When type is provided explicitly, classifier is bypassed."""
        from brain.app.cli.run import classify_task
        # Even if text says "edit", explicit type wins
        assert classify_task("edit the file", explicit_type="implement") == "implement"


# ============================================================
# build_payload tests
# ============================================================

class TestBuildPayload:
    """build_payload returns valid JSON with required fields."""

    @patch("brain.app.cli.run.build_context_block", return_value=("## Context\n- test", {"memories": 1, "guardrails": 0, "similar_tasks": 0}))
    @patch("brain.app.cli.run.log_run", return_value=42)
    @patch("brain.app.cli.run.UnitOfWork")
    def test_payload_has_required_fields(self, MockUoW, mock_log, mock_ctx):
        mock_uow = MagicMock()
        MockUoW.return_value = mock_uow
        mock_uow.__enter__ = MagicMock(return_value=mock_uow)
        mock_uow.__exit__ = MagicMock(return_value=False)

        from brain.app.cli.run import build_payload
        result = build_payload("implement rate limiting", "implement")

        assert "run_id" in result
        assert "label" in result
        assert "model" in result
        assert "thinking" in result
        assert "prompt" in result
        assert "task_type" in result
        assert "template" in result
        assert "context_injected" in result
        assert result["run_id"] == 42
        assert result["task_type"] == "implement"
        assert result["template"] == "implement"

    @patch("brain.app.cli.run.build_context_block", return_value=("", {"memories": 0, "guardrails": 0, "similar_tasks": 0}))
    @patch("brain.app.cli.run.log_run", return_value=1)
    @patch("brain.app.cli.run.UnitOfWork")
    def test_payload_model_defaults(self, MockUoW, mock_log, mock_ctx):
        mock_uow = MagicMock()
        MockUoW.return_value = mock_uow
        mock_uow.__enter__ = MagicMock(return_value=mock_uow)
        mock_uow.__exit__ = MagicMock(return_value=False)

        from brain.app.cli.run import build_payload
        result = build_payload("encode a lesson", "encode")
        assert result["model"] == "low"
        assert result["thinking"] == "off"

    @patch("brain.app.cli.run.build_context_block", return_value=("", {"memories": 0, "guardrails": 0, "similar_tasks": 0}))
    @patch("brain.app.cli.run.log_run", return_value=1)
    @patch("brain.app.cli.run.UnitOfWork")
    def test_payload_model_override(self, MockUoW, mock_log, mock_ctx):
        mock_uow = MagicMock()
        MockUoW.return_value = mock_uow
        mock_uow.__enter__ = MagicMock(return_value=mock_uow)
        mock_uow.__exit__ = MagicMock(return_value=False)

        from brain.app.cli.run import build_payload
        result = build_payload("investigate something", "investigate", model="high", thinking="high")
        assert result["model"] == "high"
        assert result["thinking"] == "high"

    @patch("brain.app.cli.run.build_context_block", return_value=("## Guardrails\n- watch out", {"memories": 0, "guardrails": 1, "similar_tasks": 0}))
    @patch("brain.app.cli.run.log_run", return_value=5)
    @patch("brain.app.cli.run.UnitOfWork")
    def test_prompt_contains_task_and_context(self, MockUoW, mock_log, mock_ctx):
        mock_uow = MagicMock()
        MockUoW.return_value = mock_uow
        mock_uow.__enter__ = MagicMock(return_value=mock_uow)
        mock_uow.__exit__ = MagicMock(return_value=False)

        from brain.app.cli.run import build_payload
        result = build_payload("investigate the bug", "investigate")
        assert "investigate" in result["prompt"].lower() or "bug" in result["prompt"].lower()

    @patch("brain.app.cli.run.build_context_block", return_value=("", {"memories": 0, "guardrails": 0, "similar_tasks": 0}))
    @patch("brain.app.cli.run.log_run", return_value=1)
    @patch("brain.app.cli.run.UnitOfWork")
    def test_label_format(self, MockUoW, mock_log, mock_ctx):
        mock_uow = MagicMock()
        MockUoW.return_value = mock_uow
        mock_uow.__enter__ = MagicMock(return_value=mock_uow)
        mock_uow.__exit__ = MagicMock(return_value=False)

        from brain.app.cli.run import build_payload
        result = build_payload("implement the auth flow", "implement")
        assert result["label"].startswith("impl-")

    @patch("brain.app.cli.run.build_context_block", return_value=("", {"memories": 0, "guardrails": 0, "similar_tasks": 0}))
    @patch("brain.app.cli.run.log_run", return_value=1)
    @patch("brain.app.cli.run.UnitOfWork")
    def test_payload_is_json_serializable(self, MockUoW, mock_log, mock_ctx):
        mock_uow = MagicMock()
        MockUoW.return_value = mock_uow
        mock_uow.__enter__ = MagicMock(return_value=mock_uow)
        mock_uow.__exit__ = MagicMock(return_value=False)

        from brain.app.cli.run import build_payload
        result = build_payload("implement something", "implement")
        # Should not raise
        json.dumps(result)


# ============================================================
# Inline allowlist tests
# ============================================================

class TestInlineAllowlist:
    """Inline allowlist correctly identifies brain queries vs work."""

    def test_brain_queries_are_inline(self):
        from brain.app.cli.run import is_inline_task
        assert is_inline_task("memory.py query 'what happened'") is True
        assert is_inline_task("memory.py context 'task'") is True
        assert is_inline_task("skills.py plan 'debug API'") is True
        assert is_inline_task("session_hooks.py wake") is True
        assert is_inline_task("session_hooks.py sleep 'done'") is True
        assert is_inline_task("session_hooks.py encode 'lesson'") is True

    def test_run_itself_is_inline(self):
        from brain.app.cli.run import is_inline_task
        assert is_inline_task("run.py run 'task'") is True

    def test_work_tasks_are_not_inline(self):
        from brain.app.cli.run import is_inline_task
        assert is_inline_task("implement rate limiting for the API") is False
        assert is_inline_task("edit config.py to add new setting") is False
        assert is_inline_task("debug the production error") is False
        assert is_inline_task("write a blog post about our architecture") is False


# ============================================================
# agent_runs persistence tests
# ============================================================

class TestRunPersistence:
    """CLI run persistence writes through agent_runs."""

    def test_log_run_inserts(self):
        mock_session = MagicMock()
        mock_session.execute.return_value.mappings.return_value.first.return_value = {"id": 42}

        from brain.app.cli.run import log_run
        run_id = log_run(
            session=mock_session,
            task_summary="implement rate limiting",
            task_type="implement",
            template_used="implement",
            model="medium",
            thinking_level="low",
            prompt_hash="abc123",
            payload_json={"test": True},
        )
        assert run_id == 42
        assert mock_session.execute.call_count == 3

    def test_log_run_with_context_metadata(self):
        mock_session = MagicMock()
        mock_session.execute.return_value.mappings.return_value.first.return_value = {"id": 10}

        from brain.app.cli.run import log_run
        run_id = log_run(
            session=mock_session,
            task_summary="investigate bug",
            task_type="investigate",
            template_used="investigate",
            model="medium",
            thinking_level="low",
            skill_name="debug",
            memories_injected=[1, 2, 3],
            guardrails_injected=["check data values"],
            similar_past_ids=[5, 6],
        )
        assert run_id == 10


# ============================================================
# Complete hook tests
# ============================================================

class TestCompleteHook:
    """complete hook updates agent_runs correctly."""

    def test_complete_updates_agent_run(self):
        mock_session = MagicMock()
        mock_session.execute.return_value.mappings.return_value.first.return_value = {
            "id": 42, "metadata": {"legacy_source": "cli.run"}
        }

        from brain.app.cli.run import complete_run
        result = complete_run(mock_session, run_id=42, outcome="success", notes="all good")

        # Should have called execute multiple times (SELECT + UPDATE)
        assert mock_session.execute.call_count >= 2
        assert result == {"run_id": 42, "outcome": "success"}

    def test_complete_with_invalid_id(self):
        mock_session = MagicMock()
        mock_session.execute.return_value.mappings.return_value.first.return_value = None

        from brain.app.cli.run import complete_run
        result = complete_run(mock_session, run_id=999, outcome="success")
        assert "error" in result


# ============================================================
# Integration-style tests (with mock DB)
# ============================================================

class TestRunIntegration:
    """End-to-end run flow with mocked dependencies."""

    @patch("brain.app.cli.run.build_context_block", return_value=("## Context\ntest", {"memories": 2, "guardrails": 1, "similar_tasks": 0}))
    @patch("brain.app.cli.run.UnitOfWork")
    def test_full_run_flow(self, MockUoW, mock_ctx):
        mock_uow = MagicMock()
        MockUoW.return_value = mock_uow
        mock_uow.__enter__ = MagicMock(return_value=mock_uow)
        mock_uow.__exit__ = MagicMock(return_value=False)
        mock_session = mock_uow.session
        mock_session.execute.return_value.mappings.return_value.first.return_value = {"id": 100}

        from brain.app.cli.run import run
        result = run("implement rate limiting")

        assert result["run_id"] == 100
        assert result["task_type"] == "implement"
        assert result["context_injected"]["memories"] == 2
