"""SkillRepository tests using in-memory SQLite."""
import re

import pytest
from datetime import datetime
from sqlalchemy import create_engine, JSON, TEXT, event
from sqlalchemy.dialects.postgresql import JSONB, ARRAY
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler, SQLiteDDLCompiler
from sqlalchemy.orm import Session

from brain.platform.db.base import Base
from brain.platform.db.models.skill import Skill
from brain.platform.db.repositories.skills import SkillRepository


def _patch_sqlite_for_pg_types():
    """Teach SQLiteTypeCompiler to handle JSONB and ARRAY as TEXT."""
    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "TEXT"
    if not hasattr(SQLiteTypeCompiler, "visit_ARRAY"):
        SQLiteTypeCompiler.visit_ARRAY = lambda self, type_, **kw: "TEXT"

    # Patch DDL compiler to strip PG-specific casts from server_default
    _original = SQLiteDDLCompiler.get_column_default_string
    def _patched(self, column, **kw):
        result = _original(self, column, **kw)
        if result:
            result = re.sub(r'::jsonb', '', result)
            result = re.sub(r'::text\[\]', '', result)
        return result
    SQLiteDDLCompiler.get_column_default_string = _patched


@pytest.fixture
def session():
    """In-memory SQLite — patches JSONB/ARRAY rendering for SQLite."""
    _patch_sqlite_for_pg_types()
    eng = create_engine("sqlite://", echo=False)
    Skill.__table__.create(eng, checkfirst=True)
    s = Session(eng)
    yield s
    s.close()


@pytest.fixture
def repo(session):
    return SkillRepository(session)


def _make_skill(repo, session, name="test-skill", **kwargs):
    defaults = {"procedure": "do the thing carefully " * 3, "version": 1,
                "confidence": 0.5, "use_count": 0, "success_count": 0,
                "failure_count": 0, "partial_count": 0}
    defaults.update(kwargs)
    skill = repo.create(name=name, **defaults)
    session.flush()
    return skill


def test_list_active_excludes_archived(repo, session):
    _make_skill(repo, session, name="active", archived=False)
    _make_skill(repo, session, name="dead", archived=True)
    result = repo.list_active()
    assert len(result) == 1
    assert result[0].name == "active"


def test_list_command_summaries_returns_skinny_rows(repo, session):
    _make_skill(
        repo,
        session,
        name="active",
        description="Visible in slash menu",
        model_tier="high",
        maturity="stable",
        use_count=4,
        success_count=3,
        archived=False,
    )
    _make_skill(repo, session, name="dead", archived=True)

    result = repo.list_command_summaries()

    assert len(result) == 1
    assert result[0].name == "active"
    assert result[0].description == "Visible in slash menu"
    assert result[0].use_count == 4


def test_get_by_name(repo, session):
    _make_skill(repo, session, name="deploy")
    found = repo.get_by_name("deploy")
    assert found is not None
    assert found.name == "deploy"


def test_get_by_name_not_found(repo):
    assert repo.get_by_name("nonexistent") is None


def test_get_by_name_or_raise_missing(repo):
    with pytest.raises(LookupError, match="not found"):
        repo.get_by_name_or_raise("nonexistent")


def test_update_tiers(repo, session):
    skill = _make_skill(repo, session)
    updated = repo.update_tiers(
        skill.id,
        model_tier="high",
        thinking_tier="xhigh",
    )
    assert updated.model_tier == "high"
    assert updated.thinking_tier == "xhigh"


def test_update_full_bumps_version(repo, session):
    skill = _make_skill(repo, session, name="versioned")
    assert skill.version == 1
    repo.update_full(skill.id, procedure="v2 procedure " * 5)
    session.flush()
    assert skill.version == 2


def test_update_full_no_version_bump_same_procedure(repo, session):
    skill = _make_skill(repo, session, name="same-proc", procedure="original " * 5)
    repo.update_full(skill.id, description="updated desc")
    session.flush()
    assert skill.version == 1


def test_update_full_updates_editable_sections(repo, session):
    skill = _make_skill(repo, session, name="sections", guardrails=[], triggers=[])
    updated = repo.update_full(
        skill.id,
        guardrails=[{"severity": "warning", "text": "check assumptions"}],
        triggers=[{"direction": "for", "pattern": "bug fix"}],
        pitfalls=["stale data"],
        refinements=["verify locally"],
    )
    session.flush()
    assert updated.guardrails[0]["text"] == "check assumptions"
    assert updated.triggers[0]["pattern"] == "bug fix"
    assert updated.pitfalls == ["stale data"]
    assert updated.refinements == ["verify locally"]


def test_update_full_rejects_provider_runtime_fields(repo, session):
    skill = _make_skill(repo, session, name="runtime")
    with pytest.raises(ValueError, match="Cannot update fields"):
        repo.update_full(skill.id, provider="openai")


def test_update_full_marks_builtin_skill_as_customized(repo, session):
    skill = _make_skill(repo, session, name="builtin-runtime", builtin=True)
    updated = repo.update_full(skill.id, model_tier="high")
    session.flush()
    assert updated.builtin is False


def test_add_guardrail(repo, session):
    _make_skill(repo, session, name="guarded", guardrails=[])
    result = repo.add_guardrail("guarded", "check logs", "warning")
    assert len(result.guardrails) == 1
    assert result.guardrails[0]["text"] == "check logs"


def test_add_trigger(repo, session):
    _make_skill(repo, session, name="routed", triggers=[])
    result = repo.add_trigger("routed", "positive", "code review")
    assert len(result.triggers) == 1
    assert result.triggers[0]["pattern"] == "code review"


def test_remove_trigger(repo, session):
    _make_skill(repo, session, name="untrigger", triggers=[
        {"direction": "positive", "pattern": "old"},
    ])
    result = repo.remove_trigger("untrigger", 0)
    assert len(result.triggers) == 0


def test_remove_trigger_invalid_index(repo, session):
    _make_skill(repo, session, name="bad-idx", triggers=[])
    with pytest.raises(ValueError, match="Invalid trigger index"):
        repo.remove_trigger("bad-idx", 5)


def test_archive(repo, session):
    skill = _make_skill(repo, session, name="archivable")
    repo.archive(skill.id)
    session.flush()
    assert skill.archived is True
    assert repo.get_by_name("archivable") is None


def test_needing_attention(repo, session):
    _make_skill(repo, session, name="ok", confidence=0.9, failure_count=0)
    _make_skill(repo, session, name="bad", confidence=0.3, failure_count=5)
    result = repo.needing_attention()
    assert len(result) == 1
    assert result[0].name == "bad"


def test_update_tiers_updates_thinking_tier(repo, session):
    skill = _make_skill(repo, session, name="mirror-update-tiers")
    updated = repo.update_tiers(skill.id, thinking_tier="high")
    assert updated.thinking_tier == "high"
