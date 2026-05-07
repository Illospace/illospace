#!/usr/bin/env python3
"""Tests for post-completion self-assessment hook logic."""

import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))

from brain.app.hooks.post_assessment_bridge import should_assess, extract_task_context, run_assessment


class TestShouldAssess(unittest.TestCase):
    """Test filtering — skip trivial messages."""

    def test_skip_no_reply(self):
        self.assertFalse(should_assess("NO_REPLY"))

    def test_skip_empty(self):
        self.assertFalse(should_assess(""))
        self.assertFalse(should_assess(None))

    def test_skip_short(self):
        self.assertFalse(should_assess("ok"))
        self.assertFalse(should_assess("👍"))

    def test_skip_reactions_only(self):
        self.assertFalse(should_assess("😂"))
        self.assertFalse(should_assess("🎯"))

    def test_accept_substantive(self):
        self.assertTrue(should_assess("Here's the fix for the API endpoint. I updated the handler to validate input."))

    def test_accept_medium_length(self):
        self.assertTrue(should_assess("I've investigated the issue and found the root cause in the database query."))

    def test_skip_whitespace_only(self):
        self.assertFalse(should_assess("   \n\t  "))


class TestExtractTaskContext(unittest.TestCase):
    """Test extracting task/outcome from message content."""

    def test_extracts_content(self):
        content = "I fixed the bug in the payment handler by adding null checks."
        ctx = extract_task_context(content)
        self.assertIn("task", ctx)
        self.assertIn("outcome", ctx)
        self.assertTrue(len(ctx["outcome"]) > 0)

    def test_truncates_long_content(self):
        content = "x" * 2000
        ctx = extract_task_context(content)
        self.assertLessEqual(len(ctx["outcome"]), 1000)

    def test_prefers_structured_payload(self):
        payload = json.dumps({
            "task": "fix the memory regression",
            "outcome": "patched the retrieval path and added tests",
        })
        ctx = extract_task_context(payload)
        self.assertEqual(ctx["task"], "fix the memory regression")
        self.assertEqual(ctx["outcome"], "patched the retrieval path and added tests")


class TestRunAssessment(unittest.TestCase):
    """Test the assessment runner handles errors gracefully."""

    @patch("brain.app.hooks.post_assessment_bridge.assess_quality")
    def test_returns_result_on_success(self, mock_assess):
        mock_assess.return_value = {
            "task_type": "code",
            "checklist": ["Run tests"],
            "warnings": [],
            "relevant_lessons": [],
            "assessment": "📋 1 checklist item(s)",
        }
        result = run_assessment("fix bug", "added null check")
        self.assertIsNotNone(result)
        self.assertEqual(result["task_type"], "code")

    @patch("brain.app.hooks.post_assessment_bridge.assess_quality")
    def test_returns_none_on_error(self, mock_assess):
        mock_assess.side_effect = Exception("DB connection failed")
        result = run_assessment("fix bug", "added null check")
        self.assertIsNone(result)

    @patch("brain.app.hooks.post_assessment_bridge.assess_quality")
    def test_critical_warnings_flagged(self, mock_assess):
        mock_assess.return_value = {
            "task_type": "code",
            "checklist": [],
            "warnings": ["Past failure: shipped without tests"],
            "relevant_lessons": [],
            "assessment": "⚠️ 1 warning(s)",
        }
        result = run_assessment("deploy", "shipped")
        self.assertIsNotNone(result)
        self.assertTrue(len(result["warnings"]) > 0)


if __name__ == "__main__":
    unittest.main()
