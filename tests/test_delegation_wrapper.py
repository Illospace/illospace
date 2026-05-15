"""Tests for services/delegation_wrapper.py — issue #70."""

import pytest
from brain.systems.runs.delegation_wrapper import wrap_delegation_prompt


class TestWrapDelegationPrompt:
    def test_valid_prompt_contains_context_and_quality_gate(self):
        result = wrap_delegation_prompt(
            "Fix the login bug",
            "Auth module repair",
            "Detailed instructions for the agent",
        )

        assert "Fix the login bug" in result
        assert "Auth module repair" in result
        assert "Detailed instructions for the agent" in result
        assert "Quality Gate" in result
        assert "acceptance criteria" in result.lower()

        no_task_result = wrap_delegation_prompt("ask", None, "prompt")
        assert "not specified" in no_task_result

    @pytest.mark.parametrize(
        ("user_ask", "prompt"),
        [
            ("", "prompt"),
            ("   ", "prompt"),
            ("ask", ""),
        ],
    )
    def test_requires_user_ask_and_prompt(self, user_ask, prompt):
        with pytest.raises(ValueError):
            wrap_delegation_prompt(user_ask, "task", prompt)
