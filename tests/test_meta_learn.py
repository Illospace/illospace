"""Tests for cli/meta_learn.py — meta-learning loop."""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))

from brain.app.cli.meta_learn import (
    author_skill,
    assess_skill,
    cross_pollinate,
    evolve_meta,
    _load_meta_state,
    _save_meta_state,
    META_STATE_PATH,
)


@pytest.fixture(autouse=True)
def temp_meta_state(tmp_path, monkeypatch):
    """Redirect meta state to temp dir."""
    fake_path = str(tmp_path / "meta_state.json")
    monkeypatch.setattr("brain.app.cli.meta_learn.META_STATE_PATH", fake_path)
    return fake_path


def _make_mock_uow():
    """Create a standard mock UnitOfWork."""
    mock_uow = MagicMock()
    mock_uow.__enter__ = MagicMock(return_value=mock_uow)
    mock_uow.__exit__ = MagicMock(return_value=False)
    mock_uow.__aenter__ = AsyncMock(return_value=mock_uow)
    mock_uow.__aexit__ = AsyncMock(return_value=False)
    mock_uow.session.execute = AsyncMock(return_value=_db_result(first=None, all_rows=[]))
    return mock_uow


def _db_result(*, first=None, all_rows=None):
    result = MagicMock()
    mappings = MagicMock()
    mappings.first.return_value = first
    mappings.all.return_value = list(all_rows or [])
    result.mappings.return_value = mappings
    return result


@pytest.fixture
def mock_uow_session():
    """Mock UnitOfWork for meta_learn."""
    mock_uow = _make_mock_uow()
    session = mock_uow.session
    with patch("brain.app.cli.meta_learn.UnitOfWork", return_value=mock_uow):
        yield session


@pytest.fixture
def mock_embed():
    """Mock embedding functions."""
    import numpy as np
    fake = np.zeros(2000, dtype=np.float32)
    fake[0] = 1.0
    with patch("brain.app.cli.meta_learn.embed_document", return_value=fake), \
         patch("brain.app.cli.meta_learn.embed_query", return_value=fake), \
         patch("brain.app.cli.meta_learn._vec_to_pg", return_value="[1,0,...]"):
        yield fake


# ---- author_skill tests ----

class TestAuthorSkill:
    async def test_rejects_vague_procedure(self, mock_uow_session, mock_embed):
        result = await author_skill("test", "desc", "do good work", "success rate above 80%")
        assert not result["approved"]
        assert any("vague" in f.lower() or "Vague" in f for f in result["feedback"])

    async def test_rejects_short_procedure(self, mock_uow_session, mock_embed):
        result = await author_skill("test", "desc", "step one", "success rate above 80%")
        assert not result["approved"]
        assert any("steps" in f.lower() for f in result["feedback"])

    async def test_rejects_unmeasurable_criteria(self, mock_uow_session, mock_embed):
        result = await author_skill(
            "test", "desc",
            "First analyze the code. Then write tests. Then review output. Finally deploy.",
            "it should work well"
        )
        assert not result["approved"]
        assert any("measurable" in f.lower() for f in result["feedback"])

    async def test_approves_good_skill(self, mock_uow_session, mock_embed):
        session = mock_uow_session
        # _check_skill_overlap: no overlap found
        # _create_skill_via_db: returns id from INSERT RETURNING
        call_count = [0]

        def smart_execute(sql, params=None):
            call_count[0] += 1
            result = MagicMock()
            sql_str = str(sql)
            if "FROM skills WHERE" in sql_str and "embedding" in sql_str:
                # overlap check
                result.mappings.return_value.first.return_value = None
            elif "INSERT INTO skills" in sql_str:
                result.mappings.return_value.first.return_value = {"id": 42}
            else:
                result.mappings.return_value.first.return_value = None
                result.mappings.return_value.all.return_value = []
            return result

        session.execute = AsyncMock(side_effect=smart_execute)

        with patch("brain.systems.skills.gate.enforce_gate"):
            result = await author_skill(
                "deploy",
                "Deploy to production",
                "First run tests. Then build container. Then push to registry. Finally update k8s.",
                "Completes within 10 minutes. Zero errors in logs. Success rate above 95%."
            )
        assert result["approved"]
        assert result["skill_id"] == 42

    async def test_rejects_overlapping_skill(self, mock_uow_session, mock_embed):
        session = mock_uow_session

        def smart_execute(sql, params=None):
            result = MagicMock()
            sql_str = str(sql)
            if "FROM skills WHERE" in sql_str and "embedding" in sql_str:
                result.mappings.return_value.first.return_value = {"name": "existing-skill", "similarity": 0.95}
            else:
                result.mappings.return_value.first.return_value = None
            return result

        session.execute = AsyncMock(side_effect=smart_execute)

        result = await author_skill(
            "test-skill", "desc",
            "Step one analyze. Step two implement. Step three validate. Step four ship.",
            "Success rate above 90%. Completes within 5 minutes."
        )
        assert not result["approved"]
        assert any("overlap" in f.lower() for f in result["feedback"])

    async def test_records_decision(self, mock_uow_session, mock_embed, temp_meta_state):
        session = mock_uow_session

        def smart_execute(sql, params=None):
            result = MagicMock()
            sql_str = str(sql)
            if "FROM skills WHERE" in sql_str and "embedding" in sql_str:
                result.mappings.return_value.first.return_value = None
            elif "INSERT INTO skills" in sql_str:
                result.mappings.return_value.first.return_value = {"id": 1}
            else:
                result.mappings.return_value.first.return_value = None
            return result

        session.execute = AsyncMock(side_effect=smart_execute)

        with patch("brain.systems.skills.gate.enforce_gate"):
            await author_skill(
                "s", "d",
                "One step. Two step. Three step. Four step.",
                "Success rate above 80%. Zero failures."
            )
        state = _load_meta_state()
        assert len(state["author_decisions"]) == 1


# ---- assess_skill tests ----

class TestAssessSkill:
    async def test_skill_not_found(self, mock_uow_session):
        session = mock_uow_session
        session.execute = AsyncMock(return_value=_db_result(first=None))

        result = await assess_skill("nonexistent")
        assert "error" in result

    async def test_healthy_skill(self, mock_uow_session, temp_meta_state):
        session = mock_uow_session
        call_count = [0]

        def smart_execute(sql, params=None):
            call_count[0] += 1
            result = MagicMock()
            sql_str = str(sql)
            if "FROM skills WHERE" in sql_str:
                result.mappings.return_value.first.return_value = {
                    "id": 1, "name": "develop", "maturity": "proficient",
                    "confidence": 0.8, "use_count": 20, "success_count": 18,
                    "failure_count": 2, "partial_count": 0, "pitfalls": [],
                    "refinements": [], "last_used": datetime.now(timezone.utc),
                }
            elif "FROM skill_executions" in sql_str:
                result.mappings.return_value.all.return_value = [
                    {"outcome": "success", "cnt": 5, "avg_dur": 120.0},
                ]
            else:
                result.mappings.return_value.first.return_value = None
                result.mappings.return_value.all.return_value = []
            return result

        session.execute = AsyncMock(side_effect=smart_execute)

        result = await assess_skill("develop")
        assert result["status"] == "healthy"
        assert result["success_rate"] == 0.9

    async def test_dormant_skill(self, mock_uow_session, temp_meta_state):
        session = mock_uow_session

        def smart_execute(sql, params=None):
            result = MagicMock()
            sql_str = str(sql)
            if "FROM skills WHERE" in sql_str:
                result.mappings.return_value.first.return_value = {
                    "id": 1, "name": "old-skill", "maturity": "emerging",
                    "confidence": 0.3, "use_count": 5, "success_count": 3,
                    "failure_count": 2, "partial_count": 0, "pitfalls": [],
                    "refinements": [],
                    "last_used": datetime.now(timezone.utc) - timedelta(days=60),
                }
            elif "FROM skill_executions" in sql_str:
                result.mappings.return_value.all.return_value = []
            else:
                result.mappings.return_value.first.return_value = None
                result.mappings.return_value.all.return_value = []
            return result

        session.execute = AsyncMock(side_effect=smart_execute)

        result = await assess_skill("old-skill")
        assert result["status"] == "dormant"

    async def test_underperforming_skill(self, mock_uow_session, temp_meta_state):
        session = mock_uow_session

        def smart_execute(sql, params=None):
            result = MagicMock()
            sql_str = str(sql)
            if "FROM skills WHERE" in sql_str:
                result.mappings.return_value.first.return_value = {
                    "id": 1, "name": "bad-skill", "maturity": "developing",
                    "confidence": 0.4, "use_count": 10, "success_count": 7,
                    "failure_count": 3, "partial_count": 0, "pitfalls": [],
                    "refinements": [], "last_used": datetime.now(timezone.utc),
                }
            elif "FROM skill_executions" in sql_str:
                result.mappings.return_value.all.return_value = [
                    {"outcome": "success", "cnt": 1, "avg_dur": 60.0},
                    {"outcome": "failure", "cnt": 3, "avg_dur": 90.0},
                ]
            else:
                result.mappings.return_value.first.return_value = None
                result.mappings.return_value.all.return_value = []
            return result

        session.execute = AsyncMock(side_effect=smart_execute)

        result = await assess_skill("bad-skill")
        assert result["status"] in ("underperforming", "failing")


# ---- cross_pollinate tests ----

class TestCrossPollinate:
    async def test_insufficient_skills(self, mock_uow_session):
        session = mock_uow_session
        session.execute = AsyncMock(return_value=_db_result(all_rows=[
            {"id": 1, "name": "only-one", "description": "", "procedure": "",
             "pitfalls": [], "refinements": [], "use_count": 5,
             "success_count": 4, "failure_count": 1, "maturity": "developing",
             "confidence": 0.5}
        ]))
        result = await cross_pollinate()
        assert "Need at least 2" in result["notes"][0]

    async def test_finds_shared_pitfalls(self, mock_uow_session):
        session = mock_uow_session
        session.execute = AsyncMock(return_value=_db_result(all_rows=[
            {"id": 1, "name": "skill-a", "description": "", "procedure": "",
             "pitfalls": ["timeout errors"], "refinements": [],
             "use_count": 10, "success_count": 8, "failure_count": 2,
             "maturity": "proficient", "confidence": 0.8},
            {"id": 2, "name": "skill-b", "description": "", "procedure": "",
             "pitfalls": ["timeout errors"], "refinements": [],
             "use_count": 5, "success_count": 3, "failure_count": 2,
             "maturity": "developing", "confidence": 0.4},
        ]))
        result = await cross_pollinate()
        assert len(result["shared_pitfalls"]) >= 1


# ---- evolve_meta tests ----

class TestEvolveMeta:
    async def test_no_data(self, temp_meta_state):
        result = await evolve_meta()
        assert any("insufficient" in c.lower() for c in result["changes"])

    async def test_tightens_on_bad_approvals(self, mock_uow_session, temp_meta_state):
        state = _load_meta_state()
        state["author_decisions"] = [
            {"name": f"s{i}", "approved": True, "skill_id": i,
             "timestamp": datetime.now(timezone.utc).isoformat(), "feedback": []}
            for i in range(1, 6)
        ]
        _save_meta_state(state)

        session = mock_uow_session
        # All approved skills are underperforming
        session.execute = AsyncMock(return_value=_db_result(all_rows=[
            {"id": i, "use_count": 5, "success_count": 1, "failure_count": 4}
            for i in range(1, 6)
        ]))

        result = await evolve_meta()
        assert result["current_criteria"]["min_procedure_steps"] > 3

    async def test_relaxes_on_good_approvals(self, mock_uow_session, temp_meta_state):
        state = _load_meta_state()
        state["author_decisions"] = [
            {"name": f"s{i}", "approved": True, "skill_id": i,
             "timestamp": datetime.now(timezone.utc).isoformat(), "feedback": []}
            for i in range(1, 6)
        ]
        _save_meta_state(state)

        session = mock_uow_session
        session.execute = AsyncMock(return_value=_db_result(all_rows=[
            {"id": i, "use_count": 10, "success_count": 9, "failure_count": 1}
            for i in range(1, 6)
        ]))

        result = await evolve_meta()
        assert result["current_criteria"]["min_procedure_steps"] <= 3
