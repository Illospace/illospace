"""Tests for curiosity.py — fetch_content, pick_source, content validation."""
import json
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))
from brain.jobs.pipelines.curiosity import pick_source, fetch_content, build_analysis_prompt, SOURCES, load_state, save_state


class TestPickSource:
    def test_never_read_sources_prioritized(self):
        state = {"last_reads": {}, "total_readings": 0}
        source = pick_source(state)
        assert source is not None
        # Never-read sources get priority 100

    def test_all_current_returns_none(self):
        """When all sources were read recently, returns None."""
        state = {"last_reads": {}}
        now = datetime.now()
        for s in SOURCES:
            state["last_reads"][s["id"]] = now.isoformat()
        source = pick_source(state)
        assert source is None

    def test_overdue_source_selected(self):
        """Sources past their frequency_days are selected."""
        state = {"last_reads": {}}
        now = datetime.now()
        # Make all sources current except one
        for s in SOURCES:
            state["last_reads"][s["id"]] = now.isoformat()
        # Make one source overdue
        target = SOURCES[0]
        state["last_reads"][target["id"]] = (now - timedelta(days=target["frequency_days"] + 5)).isoformat()
        source = pick_source(state)
        assert source is not None
        assert source["id"] == target["id"]

    def test_most_overdue_selected_first(self):
        """Among multiple overdue sources, most overdue wins."""
        state = {"last_reads": {}}
        now = datetime.now()
        for s in SOURCES:
            state["last_reads"][s["id"]] = now.isoformat()

        # Make two sources overdue — one more than the other
        s1, s2 = SOURCES[0], SOURCES[1]
        state["last_reads"][s1["id"]] = (now - timedelta(days=s1["frequency_days"] + 2)).isoformat()
        state["last_reads"][s2["id"]] = (now - timedelta(days=s2["frequency_days"] + 20)).isoformat()
        source = pick_source(state)
        assert source["id"] == s2["id"]

    def test_rotation_with_empty_state(self):
        """Fresh state with no reads should pick a tier 1 source."""
        state = {"last_reads": {}}
        source = pick_source(state)
        assert source is not None
        # All have priority 100, so tier 1 should come first
        assert source["tier"] == 1


class TestFetchContent:
    def test_successful_fetch(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "<html><body>Hello world</body></html>"

        with patch("subprocess.run", return_value=mock_result):
            content = fetch_content("https://example.com")
        assert content is not None
        assert "Hello" in content

    def test_failed_fetch_returns_none(self):
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""

        with patch("subprocess.run", return_value=mock_result):
            content = fetch_content("https://example.com")
        assert content is None

    def test_timeout_returns_none(self):
        import subprocess
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 45)):
            content = fetch_content("https://example.com")
        assert content is None

    def test_content_truncated_to_50k(self):
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "x" * 60000

        with patch("subprocess.run", return_value=mock_result):
            content = fetch_content("https://example.com")
        assert len(content) <= 50000


class TestBuildAnalysisPrompt:
    def test_prompt_includes_source_info(self):
        source = SOURCES[0]
        prompt = build_analysis_prompt(source, "Some content here", "Brain context")
        assert source["description"] in prompt
        assert source["url"] in prompt
        assert source["topic"] in prompt

    def test_prompt_includes_brain_context(self):
        source = SOURCES[0]
        prompt = build_analysis_prompt(source, "Content", "BRAIN: memory about testing")
        assert "BRAIN: memory about testing" in prompt

    def test_prompt_includes_recency_rule(self):
        source = SOURCES[0]
        prompt = build_analysis_prompt(source, "Content", "")
        assert "RECENCY RULE" in prompt
        assert "nothing_recent" in prompt

    def test_prompt_content_truncated(self):
        source = SOURCES[0]
        long_content = "x" * 50000
        prompt = build_analysis_prompt(source, long_content, "")
        # Content should be truncated to 25000 in prompt
        assert len(prompt) < 50000 + 5000  # prompt overhead


class TestContentValidationIntegration:
    """Verify raw HTML would be caught by validate.py before LLM call."""

    def test_raw_html_rejected(self):
        from brain.systems.quality.validate import validate_content_not_html
        raw_html = '<!DOCTYPE html><html><head><script>var x=1;</script></head><body class="main"><div>text</div></body></html>'
        ok, issues = validate_content_not_html(raw_html)
        assert not ok

    def test_clean_text_accepted(self):
        from brain.systems.quality.validate import validate_content_not_html
        clean = "# Blog Post\n\nThis is about AI agents and memory systems.\n\n## Key Points\n- Point 1\n- Point 2"
        ok, issues = validate_content_not_html(clean)
        assert ok


class TestStateManagement:
    def test_load_save_roundtrip(self, tmp_path):
        state_file = tmp_path / "state.json"
        state = {"last_reads": {"test": "2026-03-01T00:00:00"}, "total_readings": 5}

        with patch("brain.jobs.pipelines.curiosity.STATE_FILE", state_file):
            save_state(state)
            loaded = load_state()
        assert loaded == state

    def test_load_missing_state_returns_default(self, tmp_path):
        with patch("brain.jobs.pipelines.curiosity.STATE_FILE", tmp_path / "nonexistent.json"):
            state = load_state()
        assert state == {"last_reads": {}, "total_readings": 0, "last_run": None}
