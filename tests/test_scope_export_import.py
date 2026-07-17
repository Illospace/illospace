"""Tests for export scrubbing and import parsing."""
import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))

from brain.app.cli.brain_export import regex_scrub, scrub_content


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
    """Import only supports explicit reconstructive export bundles."""

    def test_workspace_import_helpers_are_removed(self):
        import brain.app.cli.brain_import as brain_import

        assert hasattr(brain_import, "import_from_export")
        assert not hasattr(brain_import, "import_from_workspace")
        assert not hasattr(brain_import, "_parse_markdown_sections")

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
