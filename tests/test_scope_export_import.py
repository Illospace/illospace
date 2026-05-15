"""Tests for scope classifier, export scrubbing, and import parsing."""
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))

from brain.systems.memory.scope import classify_scope
from brain.app.cli.brain_export import regex_scrub, scrub_content


# ============================================================
# Scope classifier tests
# ============================================================

class TestClassifyScope:
    """Test the heuristic scope classifier."""

    def test_lesson_without_personal_info_is_universal(self):
        assert classify_scope(
            "Always verify data values from the source of truth before coding",
            "lesson"
        ) == "universal"

    def test_lesson_with_name_is_personal(self):
        assert classify_scope(
            "Alex prefers robust solutions over quick patches",
            "lesson"
        ) == "personal"

    def test_session_type_is_personal(self):
        assert classify_scope(
            "Discussed architecture patterns for the platform",
            "session"
        ) == "personal"

    def test_daily_log_is_personal(self):
        assert classify_scope(
            "Fixed several bugs in the API layer today",
            "daily_log"
        ) == "personal"

    def test_principle_type_is_universal(self):
        assert classify_scope(
            "DRY — extract, abstract, reuse. Never copy-paste.",
            "principle"
        ) == "universal"

    def test_ip_address_is_personal(self):
        assert classify_scope(
            "Connect to the server at 192.168.1.100 for debugging",
            "lesson"
        ) == "personal"

    def test_token_is_personal(self):
        assert classify_scope(
            "Use ghp_abc123def456 for authentication",
            "lesson"
        ) == "personal"

    def test_email_is_personal(self):
        assert classify_scope(
            "Send the report to user@example.com",
            "lesson"
        ) == "personal"

    def test_generic_engineering_principle_is_universal(self):
        assert classify_scope(
            "When debugging, always trace the data path end-to-end. "
            "Never assume values — verify them.",
            "lesson"
        ) == "universal"

    def test_illo_reference_is_personal(self):
        assert classify_scope(
            "The illo platform uses a microservices architecture",
            "lesson"
        ) == "personal"

    def test_file_path_is_personal(self):
        assert classify_scope(
            "Check /home/example/projects/backend/main.py for the entry point",
            "lesson"
        ) == "personal"

    def test_dream_type_without_personal_is_universal(self):
        assert classify_scope(
            "Connection between error handling patterns and resilience architecture",
            "dream"
        ) == "universal"

    def test_emotion_type_is_personal(self):
        assert classify_scope(
            "Felt satisfied with the debugging approach",
            "emotion"
        ) == "personal"

    def test_ambiguous_defaults_to_personal(self):
        assert classify_scope(
            "Something happened today that was interesting",
            "observation"
        ) == "personal"

    def test_date_reference_is_personal(self):
        assert classify_scope(
            "on 2026-03-02 we shipped the new feature",
            "lesson"
        ) == "personal"

    def test_universal_patterns_boost(self):
        """Multiple universal signals without personal info → universal."""
        assert classify_scope(
            "Best practice: always use a strategy pattern when handling "
            "multiple error types in an architecture",
            "observation"
        ) == "universal"


# ============================================================
# Regex scrubbing tests
# ============================================================

class TestRegexScrub:
    """Test the regex scrubbing layer."""

    def test_scrub_ip(self):
        assert "<IP_ADDRESS>" in regex_scrub("Server at 10.0.0.1 is down")

    def test_scrub_token(self):
        assert "<TOKEN>" in regex_scrub("Use ghp_abc123 for auth")

    def test_scrub_email(self):
        assert "<EMAIL>" in regex_scrub("Contact user@test.com")

    def test_scrub_name(self):
        result = regex_scrub("Alex said to fix it")
        assert "Alex" not in result
        assert "the user" in result

    def test_scrub_company(self):
        result = regex_scrub("The illo.ai platform handles photos")
        assert "illo" not in result.lower()

    def test_scrub_file_path(self):
        result = regex_scrub("Check /home/example/code/main.py")
        assert "<FILE_PATH>" in result

    def test_preserves_generic_content(self):
        text = "Always verify assumptions before shipping code"
        assert regex_scrub(text) == text

    def test_scrub_bearer_token(self):
        assert "<TOKEN>" in regex_scrub("Bearer sk-123abc456")


class TestScrubContent:
    """Test the full scrubbing pipeline."""

    def test_skip_llm_uses_regex_only(self):
        result = scrub_content("Alex at 192.168.1.1", skip_llm=True)
        assert "Alex" not in result
        assert "192.168" not in result

    @patch("brain.app.cli.brain_export.llm_scrub")
    def test_calls_llm_by_default(self, mock_llm):
        mock_llm.return_value = "A universal lesson about testing"
        result = scrub_content("Alex learned about testing at Example")
        mock_llm.assert_called_once()
        assert result == "A universal lesson about testing"


# ============================================================
# Import parsing tests
# ============================================================

class TestImportParsing:
    """Test markdown parsing for workspace import."""

    def test_parse_markdown_sections(self, tmp_path):
        from brain.app.cli.brain_import import _parse_markdown_sections

        md = tmp_path / "test.md"
        md.write_text(
            "# Title\n\n"
            "## Section One\n\n"
            "This is a paragraph with enough content to be a memory chunk for testing.\n\n"
            "## Section Two\n\n"
            "Another section with substantial content that should be parsed into memories.\n"
        )
        result = _parse_markdown_sections(str(md), 'lesson')
        assert len(result) >= 2
        assert all(r['type'] == 'lesson' for r in result)

    def test_skips_tiny_fragments(self, tmp_path):
        from brain.app.cli.brain_import import _parse_markdown_sections

        md = tmp_path / "tiny.md"
        md.write_text("# Title\n\n## A\n\nToo short.\n")
        result = _parse_markdown_sections(str(md), 'lesson')
        assert len(result) == 0

    async def test_import_from_workspace_dry_run(self, tmp_workspace):
        from brain.app.cli.brain_import import import_from_workspace
        stats = await import_from_workspace(str(tmp_workspace), dry_run=True)
        assert stats['total'] > 0
        assert stats['universal'] + stats['personal'] == stats['total']

    async def test_import_from_export_dry_run(self, tmp_path):
        from brain.app.cli.brain_import import import_from_export

        # Create a minimal export
        (tmp_path / 'memories.jsonl').write_text(
            json.dumps({
                'content': 'Always test before shipping code to production environments',
                'memory_type': 'lesson',
                'salience': 7.0,
                'tags': ['testing'],
                'source': 'starter_kit',
                'original_scope': 'universal',
            }) + '\n'
        )
        (tmp_path / 'skills.json').write_text(json.dumps([{
            'name': 'test_skill',
            'description': 'A test skill',
            'procedure': 'Do the thing',
            'pitfalls': [],
            'refinements': [],
            'triggers': [],
        }]))

        stats = await import_from_export(str(tmp_path), dry_run=True)
        # dry_run for memories prints but doesn't count (no add_memory call)
        assert stats['skills'] == 1
