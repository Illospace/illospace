"""Tests for self_assess.py — post-task quality assessment."""

import json
import os
import subprocess
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))

from brain.app.hooks.self_assess import assess_quality, classify_task_type, get_checklist


class TestClassifyTaskType:
    def test_code_keywords(self):
        assert classify_task_type("fix the bug in api.py") == "code"
        assert classify_task_type("implement new endpoint") == "code"
        assert classify_task_type("refactor the database layer") == "code"
        assert classify_task_type("write tests for auth module") == "code"

    def test_investigation_keywords(self):
        assert classify_task_type("investigate why users can't login") == "investigation"
        assert classify_task_type("debug the memory leak") == "investigation"
        assert classify_task_type("analyze error rates in prod") == "investigation"
        assert classify_task_type("check why sentry shows 500s") == "investigation"

    def test_delegation_keywords(self):
        assert classify_task_type("delegate review to child agent") == "delegation"
        assert classify_task_type("spawn agent to handle PR") == "delegation"
        assert classify_task_type("child agent should handle this") == "delegation"

    def test_generic_fallback(self):
        assert classify_task_type("hello world") == "generic"
        assert classify_task_type("") == "generic"

    def test_case_insensitive(self):
        assert classify_task_type("FIX THE BUG") == "code"
        assert classify_task_type("INVESTIGATE the issue") == "investigation"

    def test_delegation_priority(self):
        assert classify_task_type("delegate the fix to child agent") == "delegation"


class TestGetChecklist:
    def test_code_checklist(self):
        items = get_checklist("code")
        assert len(items) > 0
        assert any("test" in i.lower() for i in items)

    def test_investigation_checklist(self):
        items = get_checklist("investigation")
        assert len(items) > 0
        assert any("data" in i.lower() for i in items)

    def test_delegation_checklist(self):
        items = get_checklist("delegation")
        assert len(items) > 0
        assert any("child agent" in i.lower() for i in items)

    def test_generic_has_base(self):
        items = get_checklist("generic")
        assert len(items) > 0


class TestAssessQuality:
    @patch("brain.app.hooks.self_assess.get_brain_context")
    def test_returns_required_keys(self, mock_brain):
        mock_brain.return_value = {"memories": [], "warnings": [], "guardrails": []}
        result = assess_quality("fix bug in api", "added null check")
        for key in ("task_type", "checklist", "warnings", "relevant_lessons", "assessment"):
            assert key in result

    @patch("brain.app.hooks.self_assess.get_brain_context")
    def test_task_type_detected(self, mock_brain):
        mock_brain.return_value = {"memories": [], "warnings": [], "guardrails": []}
        result = assess_quality("investigate login failures", "found root cause")
        assert result["task_type"] == "investigation"

    @patch("brain.app.hooks.self_assess.get_brain_context")
    def test_warnings_passed_through(self, mock_brain):
        mock_brain.return_value = {"memories": [], "warnings": ["Always verify data"], "guardrails": []}
        result = assess_quality("fix bug", "done")
        assert "Always verify data" in result["warnings"]
        assert result["assessment"] == "concerns"

    @patch("brain.app.hooks.self_assess.get_brain_context")
    def test_lessons_from_memories(self, mock_brain):
        mock_brain.return_value = {
            "memories": [{"content": "Always run tests", "type": "lesson", "salience": 8, "similarity": 0.7}],
            "warnings": [], "guardrails": [],
        }
        result = assess_quality("fix bug", "done")
        assert "Always run tests" in result["relevant_lessons"]

    @patch("brain.app.hooks.self_assess.get_brain_context")
    def test_empty_inputs(self, mock_brain):
        mock_brain.return_value = {"memories": [], "warnings": [], "guardrails": []}
        result = assess_quality("", "")
        assert result["task_type"] == "generic"
        assert result["assessment"] == "pass"

    @patch("brain.app.hooks.self_assess.get_brain_context")
    def test_guardrails_become_warnings(self, mock_brain):
        mock_brain.return_value = {
            "memories": [], "warnings": [],
            "guardrails": [{"skill": "coding", "failure": "forgot tests"}],
        }
        result = assess_quality("fix bug", "done")
        assert any("forgot tests" in w for w in result["warnings"])
        assert result["assessment"] == "concerns"


class TestCLI:
    def test_cli_outputs_json(self):
        env = os.environ.copy()
        env["SELF_ASSESS_NO_BRAIN"] = "1"
        result = subprocess.run(
            [sys.executable, "-m", "brain.app.hooks.self_assess", "test task", "test outcome"],
            capture_output=True, text=True,
            cwd=os.path.join(os.path.dirname(__file__), ".."),
            env=env,
        )
        assert result.returncode == 0
        first_line = result.stdout.strip().split("\n")[0]
        data = json.loads(first_line)
        assert "task_type" in data
