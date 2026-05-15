"""Tests for the Guardian enforcement layer."""

import json
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_rule(id=1, name="test_rule", trigger_type="pre_completion",
               trigger_pattern=None, required_evidence=None,
               check_description="Test check", trust_level_required=0,
               source_lesson_ids=None, source_violation_count=0,
               times_enforced=0, times_passed=0, times_bounced=0):
    return {
        "id": id, "name": name, "description": "test",
        "trigger_type": trigger_type,
        "trigger_pattern": trigger_pattern or {},
        "required_evidence": required_evidence or ["test_execution"],
        "check_description": check_description,
        "source_lesson_ids": source_lesson_ids or [1],
        "source_violation_count": source_violation_count,
        "trust_level_required": trust_level_required,
        "times_enforced": times_enforced,
        "times_passed": times_passed,
        "times_bounced": times_bounced,
    }


def _make_trust(level=0, consecutive=0, total=0, bounced=0, user_caught=0,
                threshold=10, demotion_reason=None):
    return {
        "id": 1, "current_level": level, "consecutive_clean": consecutive,
        "total_completions": total, "total_bounced": bounced,
        "total_user_caught": user_caught, "level_up_threshold": threshold,
        "last_demotion_reason": demotion_reason, "updated_at": "2026-03-03",
    }


@pytest.fixture
def mock_guardian_db():
    """Patch UnitOfWork for guardian tests."""
    mock_uow = MagicMock()
    mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
    mock_uow.__aexit__ = AsyncMock(return_value=False)
    mock_session = mock_uow.session

    executions = []

    original_execute = mock_session.execute

    async def track_execute(sql, params=None):
        sql_str = str(sql).strip()
        executions.append({"sql": sql_str, "params": params})
        result = MagicMock()
        result.mappings.return_value.all.return_value = []
        result.mappings.return_value.first.return_value = _make_trust()
        return result

    mock_session.execute = track_execute

    with patch("brain.systems.quality.guardian.UnitOfWork", return_value=mock_uow):
        yield mock_session, executions


# ---------------------------------------------------------------------------
# load_rules
# ---------------------------------------------------------------------------

class TestLoadRules:
    async def test_load_active_rules(self, mock_guardian_db):
        session, _ = mock_guardian_db
        rules_data = [_make_rule(), _make_rule(id=2, name="rule2")]

        async def custom_execute(sql, params=None):
            result = MagicMock()
            result.mappings.return_value.all.return_value = rules_data
            result.mappings.return_value.first.return_value = _make_trust()
            return result

        session.execute = custom_execute

        from brain.systems.quality.guardian import load_rules
        rules = await load_rules()
        assert len(rules) == 2
        assert rules[0]["name"] == "test_rule"

    async def test_load_all_rules(self, mock_guardian_db):
        session, _ = mock_guardian_db
        rules_data = [_make_rule()]

        async def custom_execute(sql, params=None):
            result = MagicMock()
            result.mappings.return_value.all.return_value = rules_data
            result.mappings.return_value.first.return_value = _make_trust()
            return result

        session.execute = custom_execute

        from brain.systems.quality.guardian import load_rules
        rules = await load_rules(active_only=False)
        assert len(rules) == 1


# ---------------------------------------------------------------------------
# check_completion
# ---------------------------------------------------------------------------

class TestCheckCompletion:
    async def test_passes_when_evidence_present(self, mock_guardian_db):
        session, _ = mock_guardian_db

        call_count = [0]

        async def smart_execute(sql, params=None):
            call_count[0] += 1
            result = MagicMock()
            sql_str = str(sql)
            if "guardian_rules" in sql_str:
                result.mappings.return_value.all.return_value = [
                    _make_rule(required_evidence=["test_execution"])
                ]
            elif "trust_state" in sql_str:
                result.mappings.return_value.first.return_value = _make_trust()
            else:
                result.mappings.return_value.all.return_value = []
                result.mappings.return_value.first.return_value = _make_trust()
            return result

        session.execute = smart_execute

        from brain.systems.quality.guardian import check_completion
        allowed, violations = await check_completion(
            action_log=["ran test_execution and verified output"],
            task_context={"involves_code": True}
        )
        assert allowed is True
        assert violations == []

    async def test_bounces_when_evidence_missing(self, mock_guardian_db):
        session, _ = mock_guardian_db

        async def smart_execute(sql, params=None):
            result = MagicMock()
            sql_str = str(sql)
            if "guardian_rules" in sql_str and "SELECT" in sql_str:
                result.mappings.return_value.all.return_value = [
                    _make_rule(required_evidence=["test_execution", "pytest"])
                ]
            elif "trust_state" in sql_str:
                result.mappings.return_value.first.return_value = _make_trust()
            else:
                result.mappings.return_value.all.return_value = []
                result.mappings.return_value.first.return_value = _make_trust()
            return result

        session.execute = smart_execute

        from brain.systems.quality.guardian import check_completion
        allowed, violations = await check_completion(
            action_log=["edited file.py", "read file.py"],
            task_context={"involves_code": True}
        )
        assert allowed is False
        assert len(violations) == 1
        assert "test_execution" in violations[0]

    async def test_skips_irrelevant_pre_action_rules(self, mock_guardian_db):
        session, _ = mock_guardian_db

        async def smart_execute(sql, params=None):
            result = MagicMock()
            sql_str = str(sql)
            if "guardian_rules" in sql_str and "SELECT" in sql_str:
                result.mappings.return_value.all.return_value = [_make_rule(
                    trigger_type="pre_action",
                    trigger_pattern={"requires_code": True},
                    required_evidence=["test_execution"],
                )]
            elif "trust_state" in sql_str:
                result.mappings.return_value.first.return_value = _make_trust()
            else:
                result.mappings.return_value.all.return_value = []
                result.mappings.return_value.first.return_value = _make_trust()
            return result

        session.execute = smart_execute

        from brain.systems.quality.guardian import check_completion
        allowed, violations = await check_completion(
            action_log=["asked a question"],
            task_context={"involves_code": False}
        )
        assert allowed is True

    async def test_trust_level_bypass(self, mock_guardian_db):
        session, _ = mock_guardian_db

        async def smart_execute(sql, params=None):
            result = MagicMock()
            sql_str = str(sql)
            if "guardian_rules" in sql_str and "SELECT" in sql_str:
                result.mappings.return_value.all.return_value = [_make_rule(
                    trust_level_required=2,
                    required_evidence=["something_hard_to_find"],
                )]
            elif "trust_state" in sql_str:
                result.mappings.return_value.first.return_value = _make_trust(level=2)
            else:
                result.mappings.return_value.all.return_value = []
                result.mappings.return_value.first.return_value = _make_trust(level=2)
            return result

        session.execute = smart_execute

        from brain.systems.quality.guardian import check_completion
        allowed, violations = await check_completion(
            action_log=["did stuff"],
            task_context={}
        )
        assert allowed is True


# ---------------------------------------------------------------------------
# Trust level transitions
# ---------------------------------------------------------------------------

class TestTrustTransitions:
    async def test_clean_completion_increments(self, mock_guardian_db):
        session, executions = mock_guardian_db

        from brain.systems.quality.guardian import record_completion
        await record_completion(passed=True, violations=[], caught_by="self")

        update_sqls = [e["sql"] for e in executions if "trust_state" in e.get("sql", "")]
        assert any("consecutive_clean" in sql for sql in update_sqls)

    async def test_user_catch_demotes(self, mock_guardian_db):
        session, executions = mock_guardian_db

        from brain.systems.quality.guardian import record_completion
        await record_completion(passed=False, violations=["missed something"], caught_by="user")

        update_sqls = [e["sql"] for e in executions if "trust_state" in e.get("sql", "")]
        assert any("current_level - 1" in sql for sql in update_sqls)

    async def test_guardian_catch_resets_streak(self, mock_guardian_db):
        session, executions = mock_guardian_db

        from brain.systems.quality.guardian import record_completion
        await record_completion(passed=False, violations=["bounced"], caught_by="guardian")

        update_sqls = [e["sql"] for e in executions if "trust_state" in e.get("sql", "")]
        assert any("consecutive_clean = 0" in sql for sql in update_sqls)

    async def test_demote_function(self, mock_guardian_db):
        session, executions = mock_guardian_db

        from brain.systems.quality.guardian import demote
        await demote("test demotion reason")

        update_sqls = [e["sql"] for e in executions if "trust_state" in e.get("sql", "")]
        assert any("current_level - 1" in sql for sql in update_sqls)
        params = [e["params"] for e in executions if "trust_state" in e.get("sql", "")]
        assert any("test demotion reason" in str(p) for p in params)


# ---------------------------------------------------------------------------
# get_trust_level
# ---------------------------------------------------------------------------

class TestGetTrustLevel:
    async def test_returns_level_name(self, mock_guardian_db):
        session, _ = mock_guardian_db

        async def custom_execute(sql, params=None):
            result = MagicMock()
            result.mappings.return_value.first.return_value = _make_trust(level=2)
            return result

        session.execute = custom_execute

        from brain.systems.quality.guardian import get_trust_level
        trust = await get_trust_level()
        assert trust["level_name"] == "trusted"
        assert trust["current_level"] == 2

    async def test_empty_trust_state(self, mock_guardian_db):
        session, _ = mock_guardian_db

        async def custom_execute(sql, params=None):
            result = MagicMock()
            result.mappings.return_value.first.return_value = None
            return result

        session.execute = custom_execute

        from brain.systems.quality.guardian import get_trust_level
        trust = await get_trust_level()
        assert trust["current_level"] == 0
        assert trust["level_name"] == "probation"


# ---------------------------------------------------------------------------
# get_scout_checklist
# ---------------------------------------------------------------------------

class TestScoutChecklist:
    async def test_empty_checklist(self, mock_guardian_db):
        session, _ = mock_guardian_db

        async def custom_execute(sql, params=None):
            result = MagicMock()
            result.mappings.return_value.all.return_value = []
            return result

        session.execute = custom_execute

        from brain.systems.quality.guardian import get_scout_checklist
        md = await get_scout_checklist()
        assert "Pre-Flight Checklist" in md
        assert "No checklist items" in md

    async def test_checklist_with_items(self, mock_guardian_db):
        session, _ = mock_guardian_db

        async def custom_execute(sql, params=None):
            result = MagicMock()
            result.mappings.return_value.all.return_value = [
                {"category": "code", "check_text": "Run tests before presenting", "priority": 1},
                {"category": "code", "check_text": "Verify output", "priority": 3},
                {"category": "process", "check_text": "Call skills.py plan", "priority": 2},
            ]
            return result

        session.execute = custom_execute

        from brain.systems.quality.guardian import get_scout_checklist
        md = await get_scout_checklist()
        assert "🔴" in md  # Priority 1-2
        assert "Run tests" in md
        assert "### Code" in md
        assert "### Process" in md
