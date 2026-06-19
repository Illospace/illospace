"""Canonical memory visibility policy tests."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.dialects import postgresql

from brain.platform.db.models.reconstructive_memory import MemoryNode
from brain.platform.db.repositories.memory_visibility import (
    MemoryVisibilityContext,
    memory_is_visible,
    memory_visibility_predicate,
    memory_visibility_sql,
)


def _compile_predicate(context: MemoryVisibilityContext) -> str:
    compiled = memory_visibility_predicate(MemoryNode, context).compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": True},
    )
    return str(compiled)


def test_human_context_sees_own_private_and_org_shared_memories():
    context = MemoryVisibilityContext(user_id="user-a", org_id="org-1")

    assert memory_is_visible(
        SimpleNamespace(user_id="user-a", org_id="org-1", visibility="private"),
        context,
    )
    assert memory_is_visible(
        SimpleNamespace(user_id="user-b", org_id="org-1", visibility="org"),
        context,
    )
    assert not memory_is_visible(
        SimpleNamespace(user_id="user-b", org_id="org-1", visibility="private"),
        context,
    )
    assert not memory_is_visible(
        SimpleNamespace(user_id="user-c", org_id="org-2", visibility="org"),
        context,
    )


def test_sqlalchemy_predicate_matches_canonical_visibility_shape():
    sql = _compile_predicate(MemoryVisibilityContext(user_id="usera", org_id="org1"))

    assert "memory_nodes.user_id = 'usera'" in sql
    assert "coalesce(memory_nodes.visibility, 'private') = 'private'" in sql
    assert "memory_nodes.org_id = 'org1'" in sql
    assert "coalesce(memory_nodes.visibility, 'private') IN ('team', 'org')" in sql


def test_raw_sql_policy_is_deny_by_default_without_context():
    clause, params = memory_visibility_sql(MemoryVisibilityContext(), alias="m")

    assert clause == " AND FALSE"
    assert params == {}


def test_service_context_can_run_explicit_global_memory_maintenance():
    context = MemoryVisibilityContext.from_user(
        {"id": "service:internal-api", "principal_type": "service", "internal": True}
    )

    assert context.allow_global is True
    assert str(memory_visibility_predicate(MemoryNode, context).compile()) == "true"
    assert memory_visibility_sql(context, alias="m") == ("", {})


async def test_repository_visible_search_applies_policy_to_query():
    from brain.platform.db.repositories.reconstructive_memory import ReconstructiveMemoryCompatibilityRepository

    session = MagicMock()
    result = MagicMock()
    result.all.return_value = []
    session.scalars = AsyncMock(return_value=result)
    repo = ReconstructiveMemoryCompatibilityRepository(session)

    await repo.search_visible("roadmap", MemoryVisibilityContext(user_id="usera", org_id="org1"))

    stmt = session.scalars.call_args.args[0]
    compiled = str(
        stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "memory_nodes.archived_at IS NULL" in compiled
    assert "memory_nodes.user_id = 'usera'" in compiled
    assert "memory_nodes.org_id = 'org1'" in compiled
    assert "memory_nodes.node_kind IN ('content', 'summary', 'procedure', 'policy')" in compiled
