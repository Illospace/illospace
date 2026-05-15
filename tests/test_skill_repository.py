"""SkillRepository tests using in-memory SQLite."""
import re

import pytest
from sqlalchemy.dialects.postgresql import JSONB, ARRAY
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler, SQLiteDDLCompiler

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
async def session(async_sqlite_session_factory):
    """In-memory SQLite — patches JSONB/ARRAY rendering for SQLite."""
    _patch_sqlite_for_pg_types()
    return await async_sqlite_session_factory([Skill.__table__])


@pytest.fixture
def repo(session):
    return SkillRepository(session)


async def _make_skill(repo, session, name="test-skill", **kwargs):
    defaults = {"procedure": "do the thing carefully " * 3, "version": 1,
                "confidence": 0.5, "use_count": 0, "success_count": 0,
                "failure_count": 0, "partial_count": 0}
    defaults.update(kwargs)
    skill = await repo.a_create(name=name, **defaults)
    await session.flush()
    return skill


async def test_list_active_excludes_archived(repo, session):
    await _make_skill(repo, session, name="active", archived=False)
    await _make_skill(repo, session, name="dead", archived=True)
    result = await repo.a_list_active()
    assert len(result) == 1
    assert result[0].name == "active"


async def test_list_command_summaries_returns_skinny_rows(repo, session):
    await _make_skill(
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
    await _make_skill(repo, session, name="dead", archived=True)

    result = await repo.a_list_command_summaries()

    assert len(result) == 1
    assert result[0].name == "active"
    assert result[0].description == "Visible in slash menu"
    assert result[0].use_count == 4


async def test_update_tiers(repo, session):
    skill = await _make_skill(repo, session)
    updated = await repo.a_update_tiers(
        skill.id,
        model_tier="high",
        thinking_tier="xhigh",
    )
    assert updated.model_tier == "high"
    assert updated.thinking_tier == "xhigh"


async def test_update_full_bumps_version(repo, session):
    skill = await _make_skill(repo, session, name="versioned")
    assert skill.version == 1
    await repo.a_update_full(skill.id, procedure="v2 procedure " * 5)
    await session.flush()
    assert skill.version == 2


async def test_update_full_no_version_bump_same_procedure(repo, session):
    skill = await _make_skill(repo, session, name="same-proc", procedure="original " * 5)
    await repo.a_update_full(skill.id, description="updated desc")
    await session.flush()
    assert skill.version == 1


async def test_update_full_updates_editable_sections(repo, session):
    skill = await _make_skill(repo, session, name="sections", guardrails=[], triggers=[])
    updated = await repo.a_update_full(
        skill.id,
        guardrails=[{"severity": "warning", "text": "check assumptions"}],
        triggers=[{"direction": "for", "pattern": "bug fix"}],
        pitfalls=["stale data"],
        refinements=["verify locally"],
    )
    await session.flush()
    assert updated.guardrails[0]["text"] == "check assumptions"
    assert updated.triggers[0]["pattern"] == "bug fix"
    assert updated.pitfalls == ["stale data"]
    assert updated.refinements == ["verify locally"]


async def test_update_full_rejects_provider_runtime_fields(repo, session):
    skill = await _make_skill(repo, session, name="runtime")
    with pytest.raises(ValueError, match="Cannot update fields"):
        await repo.a_update_full(skill.id, provider="openai")


async def test_update_full_marks_builtin_skill_as_customized(repo, session):
    skill = await _make_skill(repo, session, name="builtin-runtime", builtin=True)
    updated = await repo.a_update_full(skill.id, model_tier="high")
    await session.flush()
    assert updated.builtin is False


async def test_add_guardrail(repo, session):
    await _make_skill(repo, session, name="guarded", guardrails=[])
    result = await repo.a_add_guardrail("guarded", "check logs", "warning")
    assert len(result.guardrails) == 1
    assert result.guardrails[0]["text"] == "check logs"


async def test_add_trigger(repo, session):
    await _make_skill(repo, session, name="routed", triggers=[])
    result = await repo.a_add_trigger("routed", "positive", "code review")
    assert len(result.triggers) == 1
    assert result.triggers[0]["pattern"] == "code review"


async def test_remove_trigger(repo, session):
    await _make_skill(repo, session, name="untrigger", triggers=[
        {"direction": "positive", "pattern": "old"},
    ])
    result = await repo.a_remove_trigger("untrigger", 0)
    assert len(result.triggers) == 0


async def test_remove_trigger_invalid_index(repo, session):
    await _make_skill(repo, session, name="bad-idx", triggers=[])
    with pytest.raises(ValueError, match="Invalid trigger index"):
        await repo.a_remove_trigger("bad-idx", 5)


async def test_archive(repo, session):
    skill = await _make_skill(repo, session, name="archivable")
    await repo.a_archive(skill.id)
    await session.flush()
    assert skill.archived is True
    assert await repo.a_get_by_name("archivable") is None


async def test_needing_attention(repo, session):
    await _make_skill(repo, session, name="ok", confidence=0.9, failure_count=0)
    await _make_skill(repo, session, name="bad", confidence=0.3, failure_count=5)
    result = await repo.a_needing_attention()
    assert len(result) == 1
    assert result[0].name == "bad"
