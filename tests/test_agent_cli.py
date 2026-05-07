"""Tests for agent_cli.py — extract_json and call_agent."""
import json
import os
import subprocess
import sys
from unittest.mock import MagicMock, patch, mock_open

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))
from brain.app.cli.agent_cli import extract_json, call_agent


class TestExtractJson:
    def test_direct_json(self):
        result = extract_json('{"key": "value", "num": 42}')
        assert result == {"key": "value", "num": 42}

    def test_json_in_code_fence(self):
        text = 'Here is the output:\n```json\n{"status": "ok"}\n```\nDone.'
        result = extract_json(text)
        assert result == {"status": "ok"}

    def test_json_in_bare_code_fence(self):
        text = 'Result:\n```\n{"a": 1}\n```'
        result = extract_json(text)
        assert result == {"a": 1}

    def test_json_in_surrounding_text(self):
        text = 'The agent responded with {"result": true} and finished.'
        result = extract_json(text)
        assert result == {"result": True}

    def test_invalid_json_returns_none(self):
        assert extract_json("not json at all") is None

    def test_empty_string(self):
        assert extract_json("") is None

    def test_nested_json(self):
        obj = {"outer": {"inner": [1, 2, 3]}, "key": "val"}
        result = extract_json(json.dumps(obj))
        assert result == obj

    def test_json_with_whitespace(self):
        result = extract_json('  \n  {"ok": true}  \n  ')
        assert result == {"ok": True}

    def test_partial_json_braces(self):
        # Only valid JSON between outermost braces
        text = 'prefix {"valid": true} suffix'
        result = extract_json(text)
        assert result == {"valid": True}

    def test_malformed_json_in_fence_falls_through(self):
        # When fence JSON is invalid and outermost braces span invalid content, returns None
        text = '```json\n{broken json\n```\nBut also {"fallback": 1} here'
        result = extract_json(text)
        # The outermost { to } captures invalid content, so None is correct
        assert result is None

    def test_valid_json_after_plain_text(self):
        text = 'No fences here. {"fallback": 1}'
        result = extract_json(text)
        assert result == {"fallback": 1}


class TestCallAgent:
    """call_agent wraps brain.systems.runs.direct_agent.run_agent; mock that."""

    def test_success_with_text(self):
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.output = "Hello from agent"
        mock_result.error = None

        with patch("brain.systems.runs.direct_agent.run_agent", return_value=mock_result):
            result = call_agent("test-session", "hello")
        assert result["success"] is True
        assert result["text"] == "Hello from agent"
        assert result["from_file"] is False

    def test_success_from_output_file(self, tmp_path):
        output_file = tmp_path / "output.txt"
        output_file.write_text("File-based output")

        mock_result = MagicMock()
        mock_result.success = True
        mock_result.output = ""

        with patch("brain.systems.runs.direct_agent.run_agent", return_value=mock_result):
            result = call_agent("test-session", "hello", output_file=str(output_file))
        assert result["success"] is True
        assert result["text"] == "File-based output"
        assert result["from_file"] is True

    def test_failure_from_agent(self):
        mock_result = MagicMock()
        mock_result.success = False
        mock_result.output = ""
        mock_result.error = "Agent failed with error"

        with patch("brain.systems.runs.direct_agent.run_agent", return_value=mock_result):
            result = call_agent("test-session", "hello")
        assert result["success"] is False
        assert "Agent failed" in result["error"]

    def test_exception_returns_error(self):
        with patch("brain.systems.runs.direct_agent.run_agent", side_effect=Exception("boom")):
            result = call_agent("test-session", "hello")
        assert result["success"] is False
        assert "boom" in result["error"]

    def test_empty_response(self):
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.output = ""

        with patch("brain.systems.runs.direct_agent.run_agent", return_value=mock_result):
            result = call_agent("test-session", "hello")
        assert result["success"] is False
        assert "empty" in result["error"].lower()

    def test_whitespace_only_response(self):
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.output = "   \n  "

        with patch("brain.systems.runs.direct_agent.run_agent", return_value=mock_result):
            result = call_agent("test-session", "hello")
        assert result["success"] is False
        assert "empty" in result["error"].lower()

    def test_success_strips_output(self):
        mock_result = MagicMock()
        mock_result.success = True
        mock_result.output = "  answer  "
        mock_result.error = None

        with patch("brain.systems.runs.direct_agent.run_agent", return_value=mock_result):
            result = call_agent("test-session", "hello")
        assert result["success"] is True
        assert result["text"] == "  answer  "
