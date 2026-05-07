"""Extended tests for validate.py — nightly log validation and audit."""
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch, MagicMock
from contextlib import contextmanager

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))
from brain.systems.quality.validate import (
    validate_nightly_log,
    validate_curiosity_output,
    validate_content_not_html,
    validate_sub_agent_output,
    validate_memory_count,
    audit_last_night,
    format_audit_report,
)


class TestValidateNightlyLog:
    def test_good_log_passes(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        target = date(2026, 3, 1)
        log_file = log_dir / f"nightly-{target}.log"
        log_file.write_text(
            "Starting nightly cycle\n"
            "PHASE 1: Consolidation\n"
            "PHASE 2: Skill evolution\n"
            "PHASE 3: LLM reflection\n"
            "PHASE 4: Wake-up index\n"
            "PHASE 5: Sync brain\n"
            "PHASE 6: Cleanup\n"
            "SLEEP CYCLE COMPLETE\n"
        )
        # Also need reflect output
        reflect_file = log_dir / f"reflect-output-{target}.json"
        reflect_file.write_text('{"date": "2026-03-01"}')

        with patch("brain.systems.quality.validate.LOGS_DIR", log_dir), \
             patch("brain.systems.quality.validate.PENDING_REFLECTION_PATH", tmp_path / "PENDING_REFLECTION.json"):
            ok, issues = validate_nightly_log(target)
        assert ok, f"Expected pass but got issues: {issues}"

    def test_missing_log_fails(self, tmp_path):
        with patch("brain.systems.quality.validate.LOGS_DIR", tmp_path / "logs"):
            ok, issues = validate_nightly_log(date(2026, 3, 1))
        assert not ok
        assert any("No nightly log" in i for i in issues)

    def test_incomplete_cycle_detected(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        target = date(2026, 3, 1)
        (log_dir / f"nightly-{target}.log").write_text(
            "Starting nightly cycle\nPHASE 1: done\n"  # No SLEEP CYCLE COMPLETE
        )
        with patch("brain.systems.quality.validate.LOGS_DIR", log_dir), \
             patch("brain.systems.quality.validate.PENDING_REFLECTION_PATH", tmp_path / "PENDING_REFLECTION.json"):
            ok, issues = validate_nightly_log(target)
        assert not ok
        assert any("did not complete" in i for i in issues)

    def test_error_in_log_detected(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        target = date(2026, 3, 1)
        (log_dir / f"nightly-{target}.log").write_text(
            "PHASE 1\nPHASE 2\nPHASE 3\nPHASE 4\nPHASE 5\nPHASE 6\n"
            "Traceback (most recent call last):\n  NameError: name 'get_conn' is not defined\n"
            "SLEEP CYCLE COMPLETE\n"
        )
        (log_dir / f"reflect-output-{target}.json").write_text("{}")

        with patch("brain.systems.quality.validate.LOGS_DIR", log_dir), \
             patch("brain.systems.quality.validate.PENDING_REFLECTION_PATH", tmp_path / "PENDING_REFLECTION.json"):
            ok, issues = validate_nightly_log(target)
        assert not ok
        assert any("Traceback" in i or "NameError" in i for i in issues)

    def test_missing_phases_detected(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        target = date(2026, 3, 1)
        (log_dir / f"nightly-{target}.log").write_text(
            "PHASE 1\nPHASE 2\nPHASE 5\nPHASE 6\nSLEEP CYCLE COMPLETE\n"
        )
        (log_dir / f"reflect-output-{target}.json").write_text("{}")

        with patch("brain.systems.quality.validate.LOGS_DIR", log_dir), \
             patch("brain.systems.quality.validate.PENDING_REFLECTION_PATH", tmp_path / "PENDING_REFLECTION.json"):
            ok, issues = validate_nightly_log(target)
        assert not ok
        assert any("PHASE 3" in i for i in issues)
        assert any("PHASE 4" in i for i in issues)

    def test_no_reflection_output_detected(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        target = date(2026, 3, 1)
        (log_dir / f"nightly-{target}.log").write_text(
            "PHASE 1\nPHASE 2\nPHASE 3\nPHASE 4\nPHASE 5\nPHASE 6\n"
            "SLEEP CYCLE COMPLETE\n"
        )
        # No reflect-output file and no PENDING_REFLECTION
        with patch("brain.systems.quality.validate.LOGS_DIR", log_dir), \
             patch("brain.systems.quality.validate.PENDING_REFLECTION_PATH", tmp_path / "PENDING_REFLECTION.json"):
            ok, issues = validate_nightly_log(target)
        assert not ok
        assert any("reflection" in i.lower() for i in issues)


class TestValidateCuriosityOutput:
    def test_valid_output(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        target = date(2026, 3, 1)
        (log_dir / f"curiosity-output-{target}.json").write_text(
            json.dumps({"core_claim": "test", "nothing_recent": False})
        )
        with patch("brain.systems.quality.validate.LOGS_DIR", log_dir):
            ok, issues = validate_curiosity_output(target)
        assert ok

    def test_nothing_recent_flagged(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        target = date(2026, 3, 1)
        (log_dir / f"curiosity-output-{target}.json").write_text(
            json.dumps({"nothing_recent": True})
        )
        with patch("brain.systems.quality.validate.LOGS_DIR", log_dir):
            ok, issues = validate_curiosity_output(target)
        assert not ok

    def test_missing_output(self, tmp_path):
        with patch("brain.systems.quality.validate.LOGS_DIR", tmp_path):
            ok, issues = validate_curiosity_output(date(2026, 3, 1))
        assert not ok

    def test_invalid_json(self, tmp_path):
        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        target = date(2026, 3, 1)
        (log_dir / f"curiosity-output-{target}.json").write_text("not json {broken")
        with patch("brain.systems.quality.validate.LOGS_DIR", log_dir):
            ok, issues = validate_curiosity_output(target)
        assert not ok
        assert any("JSON" in i for i in issues)


class TestAuditLastNight:
    def test_audit_report_structure(self, tmp_path):
        """audit_last_night returns proper report structure."""
        with patch("brain.systems.quality.validate.LOGS_DIR", tmp_path / "logs"), \
             patch("brain.systems.quality.validate.PENDING_REFLECTION_PATH", tmp_path / "PENDING_REFLECTION.json"), \
             patch("brain.systems.quality.validate.validate_memory_count", return_value=(True, [])):
            report = audit_last_night(date(2026, 3, 1))

        assert "date" in report
        assert "checks" in report
        assert "all_passed" in report
        assert "nightly_cycle" in report["checks"]
        assert "curiosity" in report["checks"]

    def test_audit_detects_critical_issues(self, tmp_path):
        with patch("brain.systems.quality.validate.LOGS_DIR", tmp_path / "logs"), \
             patch("brain.systems.quality.validate.PENDING_REFLECTION_PATH", tmp_path / "PENDING_REFLECTION.json"), \
             patch("brain.systems.quality.validate.validate_memory_count", return_value=(True, [])):
            report = audit_last_night(date(2026, 3, 1))

        assert not report["all_passed"]
        assert report["total_issues"] > 0

    def test_format_audit_report_all_passed(self):
        report = {"date": "2026-03-01", "all_passed": True, "checks": {}, "total_issues": 0, "critical_issues": []}
        text = format_audit_report(report)
        assert "All checks passed" in text

    def test_format_audit_report_with_issues(self):
        report = {
            "date": "2026-03-01",
            "all_passed": False,
            "total_issues": 2,
            "critical_issues": [],
            "checks": {
                "nightly_cycle": {"passed": False, "issues": ["Missing log", "Traceback found"]},
                "curiosity": {"passed": True, "issues": []},
            }
        }
        text = format_audit_report(report)
        assert "❌" in text
        assert "✅" in text
        assert "Missing log" in text


class TestValidateSubAgentOutput:
    def test_empty_output_fails(self):
        ok, issues = validate_sub_agent_output("")
        assert not ok

    def test_valid_json(self):
        ok, issues = validate_sub_agent_output('{"key": "value"}')
        assert ok

    def test_empty_json_object(self):
        ok, issues = validate_sub_agent_output('{}')
        assert not ok

    def test_json_in_code_fence_flagged(self):
        ok, issues = validate_sub_agent_output('```json\n{"key": "value"}\n```')
        assert not ok
        assert any("fence" in i.lower() for i in issues)
