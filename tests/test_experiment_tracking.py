"""Tests for experiment tracking and assessment pipeline."""
import json
import os
import sys
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))


class TestExperimentCreation:
    """Test experiment memory creation via experiment_tracking module."""

    @patch("brain.jobs.pipelines.experiment_tracking.add_memory", new_callable=AsyncMock)
    async def test_create_experiment_memory_basic(self, mock_add):
        from brain.jobs.pipelines.experiment_tracking import create_experiment_memory

        mock_add.return_value = {"id": 42}
        result = await create_experiment_memory(
            description="Added retry logic to delegation",
            hypothesis="Retry logic reduces delegation failures",
            what_changed="services/delegation.py",
            success_metric="Delegation success rate > 80%",
            data_source="test_results",
            pr_number=123,
        )

        assert result == {"id": 42}
        mock_add.assert_called_once()
        call_kwargs = mock_add.call_args[1]
        assert call_kwargs["memory_type"] == "experiment"
        assert call_kwargs["salience"] == 6.0
        assert "experiment" in call_kwargs["tags"]
        assert "EXPERIMENT:" in call_kwargs["content"]
        assert "EXPERIMENT_META:" in call_kwargs["content"]

        # Verify metadata structure
        content = call_kwargs["content"]
        meta_start = content.index("EXPERIMENT_META:") + 16
        meta = json.loads(content[meta_start:])
        assert meta["hypothesis"] == "Retry logic reduces delegation failures"
        assert meta["what_changed"] == "services/delegation.py"
        assert meta["success_metric"] == "Delegation success rate > 80%"
        assert meta["data_source"] == "test_results"
        assert meta["status"] == "active"
        assert meta["pr_number"] == 123
        assert meta["extensions"] == 0
        # assess_by should be ~7 days from now
        assess_date = date.fromisoformat(meta["assess_by"])
        assert assess_date == date.today() + timedelta(days=7)

    @patch("brain.jobs.pipelines.experiment_tracking.add_memory", new_callable=AsyncMock)
    async def test_create_experiment_no_pr(self, mock_add):
        from brain.jobs.pipelines.experiment_tracking import create_experiment_memory

        mock_add.return_value = {"id": 43}
        await create_experiment_memory(
            description="Test change",
            hypothesis="Test hypothesis",
            what_changed="test.py",
            success_metric="tests pass",
            data_source="test_results",
        )
        content = mock_add.call_args[1]["content"]
        meta_start = content.index("EXPERIMENT_META:") + 16
        meta = json.loads(content[meta_start:])
        assert "pr_number" not in meta


class TestMetadataParsing:
    """Test parsing experiment metadata from content."""

    def test_parse_experiment_meta_format(self):
        from brain.jobs.pipelines.nightly_assess import _parse_experiment_metadata

        content = 'EXPERIMENT: Test change\nEXPERIMENT_META:{"status":"active","assess_by":"2026-03-10","hypothesis":"test"}'
        meta = _parse_experiment_metadata(content)
        assert meta["status"] == "active"
        assert meta["assess_by"] == "2026-03-10"
        assert meta["hypothesis"] == "test"

    def test_parse_json_block_format(self):
        from brain.jobs.pipelines.nightly_assess import _parse_experiment_metadata

        content = 'EXPERIMENT: Test\n```json\n{"status":"active","hypothesis":"h"}\n```'
        meta = _parse_experiment_metadata(content)
        assert meta["status"] == "active"

    def test_parse_no_metadata(self):
        from brain.jobs.pipelines.nightly_assess import _parse_experiment_metadata

        content = "Just a plain experiment description"
        meta = _parse_experiment_metadata(content)
        assert meta == {}

    def test_update_content(self):
        from brain.jobs.pipelines.nightly_assess import _update_experiment_content

        content = 'EXPERIMENT: Did something\nEXPERIMENT_META:{"status":"active"}'
        new_meta = {"status": "passed", "verdict": "Tests improved"}
        result = _update_experiment_content(content, new_meta)
        assert "EXPERIMENT: Did something" in result
        assert '"status": "passed"' in result


class TestAssessmentLogic:
    """Test the assessment heuristics."""

    def test_skill_success_passed(self):
        from brain.jobs.pipelines.nightly_assess import _heuristic_assess
        assert _heuristic_assess("skill_success_rates", {"avg_success_pct": 85}) == "passed"

    def test_skill_success_failed(self):
        from brain.jobs.pipelines.nightly_assess import _heuristic_assess
        assert _heuristic_assess("skill_success_rates", {"avg_success_pct": 40}) == "failed"

    def test_skill_success_inconclusive(self):
        from brain.jobs.pipelines.nightly_assess import _heuristic_assess
        assert _heuristic_assess("skill_success_rates", {"avg_success_pct": 60}) == "inconclusive"

    def test_nightly_logs_passed(self):
        from brain.jobs.pipelines.nightly_assess import _heuristic_assess
        assert _heuristic_assess("nightly_logs", {"error_lines": 1, "total_lines": 500}) == "passed"

    def test_nightly_logs_failed(self):
        from brain.jobs.pipelines.nightly_assess import _heuristic_assess
        assert _heuristic_assess("nightly_logs", {"error_lines": 50, "total_lines": 100}) == "failed"

    def test_test_results_passed(self):
        from brain.jobs.pipelines.nightly_assess import _heuristic_assess
        assert _heuristic_assess("test_results", {"pass_rate": 98}) == "passed"

    def test_test_results_failed(self):
        from brain.jobs.pipelines.nightly_assess import _heuristic_assess
        assert _heuristic_assess("test_results", {"pass_rate": 70}) == "failed"

    def test_unknown_source_inconclusive(self):
        from brain.jobs.pipelines.nightly_assess import _heuristic_assess
        assert _heuristic_assess("unknown_source", {}) == "inconclusive"


class TestExtensionLogic:
    """Test inconclusive extension behavior."""

    @patch("brain.jobs.pipelines.nightly_assess.gather_data", new_callable=AsyncMock)
    @patch("brain.jobs.pipelines.nightly_assess.UnitOfWork")
    async def test_inconclusive_extends_assess_by(self, MockUoW, mock_gather):
        from brain.jobs.pipelines.nightly_assess import assess_single_experiment

        mock_gather.return_value = {"available": False, "metrics": {}, "summary": "No data"}
        mock_uow = MagicMock()
        MockUoW.return_value = mock_uow
        mock_uow.__enter__ = MagicMock(return_value=mock_uow)
        mock_uow.__exit__ = MagicMock(return_value=False)

        target = date(2026, 3, 10)
        exp = {
            "id": 1,
            "content": 'EXPERIMENT: Test\nEXPERIMENT_META:{"status":"active","assess_by":"2026-03-10","hypothesis":"h","data_source":"manual","extensions":0}',
            "meta": {"status": "active", "assess_by": "2026-03-10", "hypothesis": "h", "data_source": "manual", "extensions": 0},
        }

        result = await assess_single_experiment(exp, target, dry_run=True)
        assert result["verdict"] == "inconclusive"
        # Meta should be updated to extend
        assert exp["meta"]["status"] == "active"
        assert exp["meta"]["assess_by"] == "2026-03-17"
        assert exp["meta"]["extensions"] == 1

    @patch("brain.jobs.pipelines.nightly_assess.gather_data", new_callable=AsyncMock)
    @patch("brain.jobs.pipelines.nightly_assess.UnitOfWork")
    async def test_max_extensions_reached(self, MockUoW, mock_gather):
        from brain.jobs.pipelines.nightly_assess import assess_single_experiment

        mock_gather.return_value = {"available": False, "metrics": {}, "summary": "No data"}

        target = date(2026, 3, 24)
        exp = {
            "id": 2,
            "content": 'EXPERIMENT: Test\nEXPERIMENT_META:{"status":"active","assess_by":"2026-03-24","hypothesis":"h","data_source":"manual","extensions":2}',
            "meta": {"status": "active", "assess_by": "2026-03-24", "hypothesis": "h", "data_source": "manual", "extensions": 2},
        }

        result = await assess_single_experiment(exp, target, dry_run=True)
        assert result["verdict"] == "inconclusive"
        # Should NOT extend further — status stays inconclusive (not active)
        assert exp["meta"]["status"] == "inconclusive"

    @patch("brain.jobs.pipelines.nightly_assess.gather_data", new_callable=AsyncMock)
    @patch("brain.jobs.pipelines.nightly_assess.UnitOfWork")
    @patch("brain.jobs.pipelines.nightly_assess.add_memory", new_callable=AsyncMock)
    async def test_failed_creates_revert_recommendation(self, mock_add, MockUoW, mock_gather):
        from brain.jobs.pipelines.nightly_assess import assess_single_experiment

        mock_gather.return_value = {
            "available": True,
            "metrics": {"avg_success_pct": 30},
            "summary": "Bad results",
        }
        mock_uow = MagicMock()
        MockUoW.return_value = mock_uow
        mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
        mock_uow.__aexit__ = AsyncMock(return_value=False)
        mock_uow.session.execute = AsyncMock()
        mock_add.return_value = {"id": 99}

        target = date(2026, 3, 10)
        exp = {
            "id": 3,
            "content": 'EXPERIMENT: Bad change\nEXPERIMENT_META:{"status":"active","assess_by":"2026-03-10","hypothesis":"improve skills","data_source":"skill_success_rates","extensions":0,"pr_number":42}',
            "meta": {"status": "active", "assess_by": "2026-03-10", "hypothesis": "improve skills", "data_source": "skill_success_rates", "extensions": 0, "pr_number": 42},
        }

        result = await assess_single_experiment(exp, target, dry_run=False)
        assert result["verdict"] == "failed"
        # Should have created an improvement memory for revert
        mock_add.assert_called_once()
        call_kwargs = mock_add.call_args[1]
        assert "REVERT" in call_kwargs["content"]
        assert call_kwargs["memory_type"] == "improvement"
        assert "experiment-revert" in call_kwargs["tags"]
