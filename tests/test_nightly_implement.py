"""Tests for pipelines/nightly_implement.py"""
import json
import os
import sys
import tempfile
from datetime import date
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))
from brain.jobs.pipelines.nightly_implement import (
    _is_safe_path,
    classify_proposal,
    apply_proposal,
    load_pending_reflection,
    _get_processed_ids,
    _save_processed_ids,
)


class TestSafePath:
    def test_safe_path(self):
        assert _is_safe_path("core/config.py") is True
        assert _is_safe_path("tests/test_foo.py") is True

    def test_unsafe_path_traversal(self):
        assert _is_safe_path("../../etc/passwd") is False
        assert _is_safe_path("/etc/passwd") is False

    def test_unsafe_absolute(self):
        assert _is_safe_path("/tmp/evil.py") is False


class TestClassifyProposal:
    def test_structured_format(self):
        content = "FILE: core/config.py\nACTION: append\nCONTENT: # new config line"
        result = classify_proposal(content)
        assert result is not None
        assert result["action"] == "append"
        assert result["target_file"] == "core/config.py"
        assert "new config line" in result["content_patch"]

    def test_unsafe_file_rejected(self):
        content = "FILE: ../../etc/passwd\nACTION: replace\nCONTENT: hacked"
        result = classify_proposal(content)
        assert result is None

    def test_freeform_becomes_log_only(self):
        content = "We should improve the consolidation pipeline"
        result = classify_proposal(content)
        assert result is not None
        assert result["action"] == "log_only"

    def test_create_action(self):
        content = "FILE: tests/test_new.py\nACTION: create\nCONTENT: # new test"
        result = classify_proposal(content)
        assert result["action"] == "create"


class TestApplyProposal:
    def test_log_only_always_succeeds(self):
        log = []
        ok = apply_proposal({"action": "log_only", "description": "test"}, False, log)
        assert ok is True

    def test_dry_run_no_changes(self):
        log = []
        ok = apply_proposal(
            {"action": "append", "target_file": "test.txt",
             "description": "test", "content_patch": "hello"},
            True, log,
        )
        assert ok is False
        assert any("Direct write lane disabled" in l for l in log)

    def test_unsafe_path_rejected(self):
        log = []
        ok = apply_proposal(
            {"action": "append", "target_file": "/etc/passwd",
             "description": "evil", "content_patch": "x"},
            False, log,
        )
        assert ok is False

    def test_auto_exec_disabled_blocks_safe_write(self, tmp_path):
        log = []
        safe_path = tmp_path / "repo" / "notes.txt"
        safe_path.parent.mkdir(parents=True)
        safe_path.write_text("")
        with patch("brain.jobs.pipelines.nightly_implement.PROJECT_ROOT", str(tmp_path)):
            ok = apply_proposal(
                {"action": "append", "target_file": "repo/notes.txt",
                 "description": "safe", "content_patch": "hello"},
                False, log,
            )
        assert ok is False
        assert safe_path.read_text() == ""
        assert any("Direct write lane disabled" in l for l in log)


class TestNightlyImplementFlow:
    def test_main_mirrors_proposals_without_writes_or_prs(self, mock_uow, tmp_path):
        ms_dir = tmp_path / "illo_private"
        logs_dir = ms_dir / "logs"
        repo_dir = ms_dir / "repo"
        logs_dir.mkdir(parents=True)
        repo_dir.mkdir(parents=True)

        notes_file = repo_dir / "notes.txt"
        notes_file.write_text("original")

        pending_path = ms_dir / "PENDING_REFLECTION.json"
        pending_path.write_text(
            json.dumps([
                {
                    "proposal": "FILE: repo/notes.txt\nACTION: append\nCONTENT: mirrored line",
                }
            ])
        )

        candidate = MagicMock()
        candidate.id = 7
        decision = MagicMock()
        decision.decision = "recommend"
        decision.reason_code = "auto_exec_class_disabled"

        with patch("brain.jobs.pipelines.nightly_implement.UnitOfWork", return_value=mock_uow), \
             patch("brain.jobs.pipelines.nightly_implement.PROJECT_ROOT", str(ms_dir)), \
             patch("brain.jobs.pipelines.nightly_implement.PENDING_PATH", str(pending_path)), \
             patch("brain.jobs.pipelines.nightly_implement.LOG_DIR", str(logs_dir)), \
             patch("brain.jobs.pipelines.nightly_implement.gather_improvement_memories", return_value=[]), \
             patch("brain.jobs.pipelines.nightly_implement.fetch_nightly_issues", return_value=[]), \
             patch("brain.jobs.pipelines.nightly_implement.mirror_implement_proposal", return_value=(candidate, decision)) as mock_mirror, \
             patch("brain.jobs.pipelines.nightly_implement.run_tests", side_effect=AssertionError("run_tests should not run")), \
             patch("brain.jobs.pipelines.nightly_implement._save_processed_ids") as mock_save, \
             patch("sys.argv", ["nightly_implement", "--date", "2026-03-07"]):
            from brain.jobs.pipelines.nightly_implement import main as implement_main
            implement_main()

        assert notes_file.read_text() == "original"
        assert mock_mirror.called
        assert mock_save.called
        assert not pending_path.exists()
        assert (ms_dir / "PENDING_REFLECTION.json.done-2026-03-07").exists()


class TestLoadPendingReflection:
    def test_missing_file(self):
        with patch("brain.jobs.pipelines.nightly_implement.PENDING_PATH", "/nonexistent"):
            assert load_pending_reflection() == []

    def test_dict_wrapped_in_list(self, tmp_path):
        p = tmp_path / "pending.json"
        p.write_text(json.dumps({"proposal": "test"}))
        with patch("brain.jobs.pipelines.nightly_implement.PENDING_PATH", str(p)):
            result = load_pending_reflection()
            assert len(result) == 1

    def test_list_passthrough(self, tmp_path):
        p = tmp_path / "pending.json"
        p.write_text(json.dumps([{"a": 1}, {"b": 2}]))
        with patch("brain.jobs.pipelines.nightly_implement.PENDING_PATH", str(p)):
            result = load_pending_reflection()
            assert len(result) == 2


class TestProcessingLog:
    def test_round_trip(self, tmp_path):
        log_path = tmp_path / "log.json"
        with patch("brain.jobs.pipelines.nightly_implement.PROCESSING_LOG", str(log_path)):
            _save_processed_ids({1, 2, 3})
            assert _get_processed_ids() == {1, 2, 3}

    def test_empty_when_missing(self):
        with patch("brain.jobs.pipelines.nightly_implement.PROCESSING_LOG", "/nonexistent"):
            assert _get_processed_ids() == set()
