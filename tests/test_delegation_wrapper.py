"""Tests for services/delegation_wrapper.py — issue #70."""

import pytest
from brain.systems.runs.delegation_wrapper import wrap_delegation_prompt


class TestWrapDelegationPrompt:
    def test_includes_user_ask(self):
        result = wrap_delegation_prompt("Fix the login bug", "Fix auth", "Do the thing")
        assert "Fix the login bug" in result

    def test_includes_task(self):
        result = wrap_delegation_prompt("Fix it", "Auth module repair", "Instructions here")
        assert "Auth module repair" in result

    def test_includes_prompt(self):
        result = wrap_delegation_prompt("Fix it", "task", "Detailed instructions for the agent")
        assert "Detailed instructions for the agent" in result

    def test_includes_quality_gate(self):
        result = wrap_delegation_prompt("Fix it", "task", "Do work")
        assert "Quality Gate" in result
        assert "acceptance criteria" in result.lower()

    def test_empty_user_ask_raises(self):
        with pytest.raises(ValueError):
            wrap_delegation_prompt("", "task", "prompt")

    def test_empty_prompt_raises(self):
        with pytest.raises(ValueError):
            wrap_delegation_prompt("ask", "task", "")

    def test_whitespace_only_raises(self):
        with pytest.raises(ValueError):
            wrap_delegation_prompt("   ", "task", "prompt")

    def test_none_task_handled(self):
        result = wrap_delegation_prompt("ask", None, "prompt")
        assert "not specified" in result
