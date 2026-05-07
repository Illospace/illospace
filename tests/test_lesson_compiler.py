"""Tests for the Lesson Compiler."""

import json
from datetime import date
from unittest.mock import MagicMock, patch

import pytest
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))


@pytest.fixture
def mock_compiler_db():
    """Patch UnitOfWork for compiler tests."""
    mock_uow = MagicMock()
    mock_uow.__enter__ = MagicMock(return_value=mock_uow)
    mock_uow.__exit__ = MagicMock(return_value=False)
    mock_session = mock_uow.session

    executions = []

    def track_execute(sql, params=None):
        sql_str = str(sql).strip()
        executions.append({"sql": sql_str, "params": params})
        result = MagicMock()
        result.mappings.return_value.all.return_value = []
        result.mappings.return_value.first.return_value = None
        return result

    mock_session.execute = track_execute

    with patch("brain.systems.memory.lessons.UnitOfWork", return_value=mock_uow):
        yield mock_session, executions


class TestAuditLessons:
    def test_empty_day(self, mock_compiler_db):
        session, _ = mock_compiler_db

        def custom_execute(sql, params=None):
            result = MagicMock()
            result.mappings.return_value.all.return_value = []
            result.mappings.return_value.first.return_value = None
            return result

        session.execute = custom_execute

        from brain.systems.memory.lessons import audit_lessons
        report = audit_lessons(date(2026, 3, 3))
        assert report["total_lessons_today"] == 0
        assert report["violations"] == []

    def test_detects_already_compiled(self, mock_compiler_db):
        session, _ = mock_compiler_db
        call_count = [0]

        def smart_execute(sql, params=None):
            call_count[0] += 1
            result = MagicMock()
            sql_str = str(sql)
            if "FROM memories" in sql_str:
                result.mappings.return_value.all.return_value = [
                    {"id": 5, "content": "test lesson", "salience": 9.0, "tags": []}
                ]
            elif "FROM guardian_rules" in sql_str:
                result.mappings.return_value.all.return_value = [
                    {"id": 1, "name": "test_rule", "source_lesson_ids": [5]}
                ]
            else:
                result.mappings.return_value.all.return_value = []
            result.mappings.return_value.first.return_value = None
            return result

        session.execute = smart_execute

        from brain.systems.memory.lessons import audit_lessons
        report = audit_lessons(date(2026, 3, 3))
        assert report["total_lessons_today"] == 1
        assert len(report["already_compiled"]) == 1

    def test_new_compilable_lesson(self, mock_compiler_db):
        session, _ = mock_compiler_db
        call_count = [0]

        def smart_execute(sql, params=None):
            call_count[0] += 1
            result = MagicMock()
            sql_str = str(sql)
            if "FROM memories" in sql_str:
                result.mappings.return_value.all.return_value = [
                    {"id": 10, "content": "brand new lesson", "salience": 8.0, "tags": []}
                ]
            elif "FROM guardian_rules" in sql_str:
                result.mappings.return_value.all.return_value = []
            else:
                result.mappings.return_value.all.return_value = []
            result.mappings.return_value.first.return_value = None
            return result

        session.execute = smart_execute

        from brain.systems.memory.lessons import audit_lessons
        report = audit_lessons(date(2026, 3, 3))
        assert 10 in report["new_compilable"]


class TestCompileLessonToRule:
    def test_successful_compilation(self, mock_compiler_db):
        session, executions = mock_compiler_db

        call_count = [0]

        def smart_execute(sql, params=None):
            call_count[0] += 1
            sql_str = str(sql).strip()
            executions.append({"sql": sql_str, "params": params})
            result = MagicMock()
            if "FROM memories" in sql_str and "id = " in sql_str:
                # Lesson lookup
                result.mappings.return_value.first.return_value = {
                    "id": 1, "content": "Always run tests before shipping code",
                    "salience": 9.0, "tags": ["process"]
                }
            elif "FROM guardian_rules" in sql_str and "name = " in sql_str:
                # Duplicate name check
                result.mappings.return_value.first.return_value = None
            elif "INSERT INTO guardian_rules" in sql_str:
                result.mappings.return_value.first.return_value = {"id": 42}
            else:
                result.mappings.return_value.first.return_value = None
                result.mappings.return_value.all.return_value = []
            return result

        session.execute = smart_execute

        agent_response = {
            "success": True,
            "text": json.dumps({
                "compilable": True,
                "name": "run_tests_before_shipping",
                "description": "Run tests before presenting code",
                "trigger_type": "pre_completion",
                "trigger_pattern": {"requires_code": True},
                "required_evidence": ["test_execution", "pytest"],
                "check_description": "Run tests and paste output before presenting code changes",
                "category": "code",
                "priority": 1,
            }),
            "from_file": False,
            "error": None,
        }

        with patch("brain.systems.memory.lessons.call_agent", return_value=agent_response):
            from brain.systems.memory.lessons import compile_lesson_to_rule
            rule_id = compile_lesson_to_rule(1)
            assert rule_id == 42

    def test_non_compilable_lesson(self, mock_compiler_db):
        session, _ = mock_compiler_db

        def smart_execute(sql, params=None):
            result = MagicMock()
            result.mappings.return_value.first.return_value = {
                "id": 1, "content": "Some context note", "salience": 5.0, "tags": []
            }
            result.mappings.return_value.all.return_value = []
            return result

        session.execute = smart_execute

        agent_response = {
            "success": True,
            "text": json.dumps({"compilable": False}),
            "from_file": False, "error": None,
        }

        with patch("brain.systems.memory.lessons.call_agent", return_value=agent_response):
            from brain.systems.memory.lessons import compile_lesson_to_rule
            rule_id = compile_lesson_to_rule(1)
            assert rule_id is None

    def test_agent_failure(self, mock_compiler_db):
        session, _ = mock_compiler_db

        def smart_execute(sql, params=None):
            result = MagicMock()
            result.mappings.return_value.first.return_value = {
                "id": 1, "content": "Lesson", "salience": 8.0, "tags": []
            }
            result.mappings.return_value.all.return_value = []
            return result

        session.execute = smart_execute

        with patch("brain.systems.memory.lessons.call_agent", return_value={"success": False, "text": "", "from_file": False, "error": "timeout"}):
            from brain.systems.memory.lessons import compile_lesson_to_rule
            rule_id = compile_lesson_to_rule(1)
            assert rule_id is None


class TestEscalateRule:
    def test_escalate_lowers_threshold(self, mock_compiler_db):
        session, executions = mock_compiler_db

        from brain.systems.memory.lessons import escalate_rule
        escalate_rule(1)

        sqls = [e["sql"] for e in executions]
        assert any("trust_level_required" in sql for sql in sqls)
        assert any("priority" in sql for sql in sqls)


class TestGenerateChecklist:
    def test_generates_from_rules(self, mock_compiler_db):
        session, executions = mock_compiler_db

        rule_data = [
            {"id": 1, "name": "test_rule", "check_description": "Run tests",
             "trigger_type": "pre_completion", "trigger_pattern": {"requires_code": True}},
        ]
        stats_data = {"source_violation_count": 2, "times_bounced": 1}

        def smart_execute(sql, params=None):
            sql_str = str(sql).strip()
            executions.append({"sql": sql_str, "params": params})
            result = MagicMock()
            if "SELECT source_violation_count" in sql_str:
                # Stats query for priority calculation
                result.mappings.return_value.first.return_value = stats_data
            elif "FROM guardian_rules" in sql_str and "SELECT" in sql_str:
                result.mappings.return_value.all.return_value = rule_data
            else:
                result.mappings.return_value.all.return_value = []
                result.mappings.return_value.first.return_value = None
            return result

        session.execute = smart_execute

        with patch("brain.systems.memory.lessons._write_agent_checklist"), \
             patch("brain.systems.quality.guardian.get_scout_checklist", return_value="## Pre-Flight Checklist\n"):
            from brain.systems.memory.lessons import generate_checklist
            generate_checklist()

        insert_sqls = [e for e in executions if "INSERT INTO checklist_items" in e.get("sql", "")]
        assert len(insert_sqls) >= 1
