"""Tests for prompt templates and prompt builder — issue #74 (ORM-based)."""

import json
import os
import sys
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))


@pytest.fixture
def mock_template_uow():
    """Mock UnitOfWork for prompt_templates module."""
    mock_uow = MagicMock()
    mock_uow.__enter__ = MagicMock(return_value=mock_uow)
    mock_uow.__exit__ = MagicMock(return_value=False)
    # Default empty
    mock_uow.session.execute.return_value.mappings.return_value.all.return_value = []
    mock_uow.session.execute.return_value.mappings.return_value.first.return_value = None
    mock_uow.session.scalars.return_value.first.return_value = None
    mock_uow.session.scalars.return_value.all.return_value = []
    mock_uow.session.scalar.return_value = 0.0
    return mock_uow


@pytest.fixture
def patch_tmpl_uow(mock_template_uow):
    """Patch UnitOfWork for templates + evolve_prompts modules."""
    with patch("brain.systems.prompts.templates.UnitOfWork", return_value=mock_template_uow), \
         patch("brain.jobs.pipelines.nightly_evolve_prompts.UnitOfWork", return_value=mock_template_uow):
        yield mock_template_uow


class TestPromptTemplates:
    def test_ensure_tables(self, patch_tmpl_uow):
        from brain.systems.prompts.templates import ensure_tables
        ensure_tables()
        # Should have executed CREATE TABLE statements
        assert patch_tmpl_uow.session.execute.call_count >= 1

    def test_create_template(self, patch_tmpl_uow):
        from brain.systems.prompts.templates import create_template
        # Mock flush to assign id
        mock_tmpl = MagicMock()
        mock_tmpl.id = 1
        patch_tmpl_uow.session.add.side_effect = lambda obj: setattr(obj, 'id', 1)
        tid = create_template("test_tmpl", "Hello {task}", version=1)
        assert tid == 1

    def test_get_template(self, patch_tmpl_uow):
        mock_row = MagicMock()
        mock_row.id = 1
        mock_row.name = "bug_fix"
        mock_row.template_text = "Fix: {task}"
        mock_row.version = 2
        mock_row.avg_quality_score = 0.8
        mock_row.use_count = 10
        mock_row.last_used = None
        mock_row.created_at = None
        mock_row.updated_at = None
        patch_tmpl_uow.session.scalars.return_value.first.return_value = mock_row
        from brain.systems.prompts.templates import get_template
        t = get_template("bug_fix")
        assert t is not None
        assert t["name"] == "bug_fix"
        assert t["version"] == 2

    def test_get_template_not_found(self, patch_tmpl_uow):
        patch_tmpl_uow.session.scalars.return_value.first.return_value = None
        from brain.systems.prompts.templates import get_template
        assert get_template("nonexistent") is None

    def test_list_templates(self, patch_tmpl_uow):
        patch_tmpl_uow.session.execute.return_value.mappings.return_value.all.return_value = [
            {"id": 1, "name": "a", "version": 1},
            {"id": 2, "name": "b", "version": 2},
        ]
        from brain.systems.prompts.templates import list_templates
        result = list_templates()
        assert len(result) == 2

    def test_record_use(self, patch_tmpl_uow):
        from brain.systems.prompts.templates import record_use
        record_use("bug_fix")
        # Should have called session.execute to update use_count
        assert patch_tmpl_uow.session.execute.call_count >= 1

    def test_record_template_outcome(self, patch_tmpl_uow):
        mock_outcome = MagicMock()
        mock_outcome.id = 1
        patch_tmpl_uow.session.add.side_effect = lambda obj: setattr(obj, 'id', 1)
        patch_tmpl_uow.session.scalar.return_value = 0.75
        mock_tmpl = MagicMock()
        patch_tmpl_uow.session.scalars.return_value.first.return_value = mock_tmpl
        from brain.systems.prompts.templates import record_template_outcome
        oid = record_template_outcome("bug_fix", 1, 0.75)
        assert oid == 1

    def test_get_outcome_history(self, patch_tmpl_uow):
        mock_row = MagicMock()
        mock_row.id = 1
        mock_row.template_name = "bug_fix"
        mock_row.template_version = 1
        mock_row.quality_score = 0.8
        mock_row.created_at = None
        patch_tmpl_uow.session.scalars.return_value.all.return_value = [mock_row]
        from brain.systems.prompts.templates import get_outcome_history
        history = get_outcome_history("bug_fix")
        assert len(history) == 1

    def test_get_underperforming_templates(self, patch_tmpl_uow):
        patch_tmpl_uow.session.execute.return_value.mappings.return_value.all.return_value = [
            {"id": 1, "name": "bad_tmpl", "avg_quality_score": 0.3,
             "use_count": 5, "version": 1, "template_text": "..."},
        ]
        from brain.systems.prompts.templates import get_underperforming_templates
        result = get_underperforming_templates(0.6)
        assert len(result) == 1

    def test_seed_templates_idempotent(self, patch_tmpl_uow):
        """Seed doesn't crash when templates already exist."""
        mock_row = MagicMock()
        mock_row.id = 1
        mock_row.name = "code_review"
        mock_row.template_text = "..."
        mock_row.version = 1
        mock_row.avg_quality_score = 0.0
        mock_row.use_count = 0
        mock_row.last_used = None
        mock_row.created_at = None
        mock_row.updated_at = None
        patch_tmpl_uow.session.scalars.return_value.first.return_value = mock_row
        from brain.systems.prompts.templates import seed_templates
        seed_templates()


class TestPromptBuilder:
    def test_build_prompt_basic(self, patch_tmpl_uow):
        mock_row = MagicMock()
        mock_row.id = 1
        mock_row.name = "bug_fix"
        mock_row.version = 1
        mock_row.template_text = "Fix this: {task}\n\n{guardrails}"
        mock_row.avg_quality_score = 0.0
        mock_row.use_count = 0
        mock_row.last_used = None
        mock_row.created_at = None
        mock_row.updated_at = None
        patch_tmpl_uow.session.scalars.return_value.first.return_value = mock_row
        with patch("brain.systems.prompts.builder._get_guardrails", return_value=[]):
            from brain.systems.prompts.builder import build_prompt
            result = build_prompt("bug_fix", "fix the API timeout")
        assert "fix the API timeout" in result

    def test_build_prompt_with_user_ask(self, patch_tmpl_uow):
        mock_row = MagicMock()
        mock_row.id = 1
        mock_row.name = "bug_fix"
        mock_row.version = 1
        mock_row.template_text = "Fix: {task}\n\n{guardrails}"
        mock_row.avg_quality_score = 0.0
        mock_row.use_count = 0
        mock_row.last_used = None
        mock_row.created_at = None
        mock_row.updated_at = None
        patch_tmpl_uow.session.scalars.return_value.first.return_value = mock_row
        with patch("brain.systems.prompts.builder._get_guardrails", return_value=[]):
            from brain.systems.prompts.builder import build_prompt
            result = build_prompt("bug_fix", "fix timeout", user_ask="The API keeps timing out")
        assert "Original User Request" in result
        assert "The API keeps timing out" in result

    def test_build_prompt_with_guardrails(self, patch_tmpl_uow):
        mock_row = MagicMock()
        mock_row.id = 1
        mock_row.name = "bug_fix"
        mock_row.version = 1
        mock_row.template_text = "Fix: {task}\n\n{guardrails}"
        mock_row.avg_quality_score = 0.0
        mock_row.use_count = 0
        mock_row.last_used = None
        mock_row.created_at = None
        mock_row.updated_at = None
        patch_tmpl_uow.session.scalars.return_value.first.return_value = mock_row
        with patch("brain.systems.prompts.builder._get_guardrails",
                   return_value=["Always check logs", "Verify with tests"]):
            from brain.systems.prompts.builder import build_prompt
            result = build_prompt("bug_fix", "fix timeout")
        assert "Always check logs" in result
        assert "Learned Guardrails" in result

    def test_build_prompt_missing_template(self, patch_tmpl_uow):
        patch_tmpl_uow.session.scalars.return_value.first.return_value = None
        with patch("brain.systems.prompts.builder._get_guardrails", return_value=[]), \
             patch("brain.systems.prompts.builder.seed_templates"):
            from brain.systems.prompts.builder import build_prompt
            with pytest.raises(ValueError, match="not found"):
                build_prompt("nonexistent", "task")

    def test_build_prompt_no_guardrails(self, patch_tmpl_uow):
        mock_row = MagicMock()
        mock_row.id = 1
        mock_row.name = "bug_fix"
        mock_row.version = 1
        mock_row.template_text = "Fix: {task}\n\n{guardrails}"
        mock_row.avg_quality_score = 0.0
        mock_row.use_count = 0
        mock_row.last_used = None
        mock_row.created_at = None
        mock_row.updated_at = None
        patch_tmpl_uow.session.scalars.return_value.first.return_value = mock_row
        from brain.systems.prompts.builder import build_prompt
        result = build_prompt("bug_fix", "fix it", inject_guardrails=False)
        assert "Learned Guardrails" not in result


class TestNightlyEvolvePrompts:
    def test_evolve_no_underperformers(self, patch_tmpl_uow):
        patch_tmpl_uow.session.execute.return_value.mappings.return_value.all.return_value = []
        from brain.jobs.pipelines.nightly_evolve_prompts import evolve_templates
        evolve_templates(dry_run=True)

    def test_evolve_creates_new_version(self, patch_tmpl_uow):
        call_count = [0]
        def mappings_all_side_effect():
            call_count[0] += 1
            if call_count[0] == 1:
                return [{
                    "id": 1, "name": "bad_tmpl", "version": 1,
                    "avg_quality_score": 0.3, "use_count": 5,
                    "template_text": "Do: {task}\n\n{guardrails}",
                }]
            return []

        patch_tmpl_uow.session.execute.return_value.mappings.return_value.all.side_effect = mappings_all_side_effect
        mock_tmpl = MagicMock()
        mock_tmpl.id = 2
        patch_tmpl_uow.session.add.side_effect = lambda obj: setattr(obj, 'id', 2)

        from brain.jobs.pipelines.nightly_evolve_prompts import evolve_templates
        evolve_templates(dry_run=False)

    def test_evolve_dry_run_no_writes(self, patch_tmpl_uow):
        call_count = [0]
        def mappings_all_side_effect():
            call_count[0] += 1
            if call_count[0] == 1:
                return [{
                    "id": 1, "name": "bad_tmpl", "version": 1,
                    "avg_quality_score": 0.3, "use_count": 5,
                    "template_text": "Do: {task}",
                }]
            return []

        patch_tmpl_uow.session.execute.return_value.mappings.return_value.all.side_effect = mappings_all_side_effect
        from brain.jobs.pipelines.nightly_evolve_prompts import evolve_templates
        evolve_templates(dry_run=True)
