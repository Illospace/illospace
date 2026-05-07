"""Tests for output validation and anomaly detection."""
import json
import os
import pytest
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))
from brain.systems.quality.validate import (
    validate_content_not_html,
    validate_sub_agent_output,
)


class TestContentValidation:
    def test_rejects_html(self):
        ok, issues = validate_content_not_html(
            '<!DOCTYPE html><html><head></head><body><div class="x">text</div></body></html>'
        )
        assert not ok
        assert any("HTML" in i for i in issues)

    def test_accepts_markdown(self):
        ok, issues = validate_content_not_html(
            "# Research Blog\n\nThis is a clean article about AI agents.\n\n"
            "## Key Findings\n\n- Finding 1\n- Finding 2"
        )
        assert ok

    def test_accepts_plain_text(self):
        ok, issues = validate_content_not_html(
            "The memory system should validate all inputs before storing them."
        )
        assert ok

    def test_detects_html_artifacts(self):
        ok, issues = validate_content_not_html(
            '<!DOCTYPE html><html lang="en" class="module__abc"><head><meta charSet="utf-8"/>'
        )
        assert not ok
        assert any("artifact" in i.lower() for i in issues)


class TestSubAgentOutput:
    def test_rejects_empty(self):
        ok, issues = validate_sub_agent_output("")
        assert not ok
        assert any("empty" in i.lower() for i in issues)

    def test_accepts_valid_json(self):
        ok, issues = validate_sub_agent_output('{"result": "ok"}')
        assert ok

    def test_detects_json_in_markdown(self):
        ok, issues = validate_sub_agent_output(
            'Here is the result:\n```json\n{"result": "ok"}\n```'
        )
        assert not ok  # Not directly valid JSON
        assert any("code fence" in i.lower() for i in issues)

    def test_rejects_empty_json_object(self):
        ok, issues = validate_sub_agent_output('{}')
        assert not ok
        assert any("empty json" in i.lower() for i in issues)

    def test_rejects_non_json(self):
        ok, issues = validate_sub_agent_output("This is just text, no JSON here")
        assert not ok
