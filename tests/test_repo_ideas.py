"""IdeaRepository tests using in-memory SQLite."""
import uuid
from datetime import datetime, timezone

import re

import pytest
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler, SQLiteDDLCompiler

from brain.platform.db.base import Base
from brain.platform.db.models.idea import Idea, IdeaConnection, IdeaStateLog, IdeaThread
from brain.platform.db.models.org import Org, User
from brain.platform.db.repositories.ideas import (
    IdeaConnectionRepository,
    IdeaRepository,
    IdeaThreadRepository,
)

ORG_1 = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa1"
ORG_2 = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaa2"
USER_1 = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb1"
USER_2 = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbb2"


def _patch_sqlite_for_pg_types():
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


def _register_sqlite_functions(dbapi_conn, connection_record):
    """Register PG-compatible functions for SQLite."""
    dbapi_conn.create_function("NOW", 0, lambda: datetime.now(timezone.utc).isoformat())
    dbapi_conn.create_function(
        "gen_random_uuid", 0, lambda: str(uuid.uuid4())
    )


def _register_sqlite_adapters():
    import json
    import sqlite3
    sqlite3.register_adapter(list, lambda val: json.dumps(val))
    sqlite3.register_adapter(dict, lambda val: json.dumps(val))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def session(async_sqlite_session_factory):
    _patch_sqlite_for_pg_types()
    _register_sqlite_adapters()
    s = await async_sqlite_session_factory(
        [
            Org.__table__,
            User.__table__,
            Idea.__table__,
            IdeaStateLog.__table__,
            IdeaConnection.__table__,
            IdeaThread.__table__,
        ],
        connect_listener=_register_sqlite_functions,
    )
    # seed org + user for FK constraints
    org = Org(id=ORG_1, name="Test Org", slug="test-org")
    s.add(org)
    user = User(id=USER_1, org_id=ORG_1, name="Alex", email="alex@test.com")
    s.add(user)
    await s.flush()
    return s


@pytest.fixture
def repo(session):
    return IdeaRepository(session)


@pytest.fixture
def thread_repo(session):
    return IdeaThreadRepository(session)


@pytest.fixture
def conn_repo(session):
    return IdeaConnectionRepository(session)


async def _make_idea(session, **kwargs):
    defaults = {
        "id": str(uuid.uuid4()),
        "title": "Test Idea",
        "user_id": USER_1,
    }
    defaults.update(kwargs)
    idea = Idea(**defaults)
    session.add(idea)
    await session.flush()
    return idea


async def _seed_org_user(session, org_id=ORG_2, user_id=USER_2):
    org = Org(id=org_id, name=f"Test {org_id}", slug=org_id)
    user = User(id=user_id, org_id=org_id, name=f"User {user_id}", email=f"{user_id}@test.com")
    session.add_all([org, user])
    await session.flush()
    return org, user


# ---------------------------------------------------------------------------
# IdeaRepository
# ---------------------------------------------------------------------------

class TestIdeaRepository:
    def test_model_assignment(self):
        assert IdeaRepository.model is Idea

    async def test_list_active(self, repo, session):
        await _make_idea(session)
        from datetime import datetime, timezone
        await _make_idea(session, archived_at=datetime.now(timezone.utc))
        result = await repo.a_list_active()
        assert len(result) == 1

    async def test_list_by_org(self, repo, session):
        await _make_idea(session, org_id=ORG_1)
        await _make_idea(session, org_id=None)
        result = await repo.a_list_by_org(ORG_1)
        assert len(result) == 1

    async def test_get_for_org_returns_only_matching_org(self, repo, session):
        idea = await _make_idea(session, org_id=ORG_1)
        assert await repo.a_get_for_org(idea.id, ORG_1) == idea
        assert await repo.a_get_for_org(idea.id, ORG_2) is None

    async def test_list_active_for_org_excludes_other_orgs(self, repo, session):
        await _seed_org_user(session)
        await _make_idea(session, org_id=ORG_1)
        await _make_idea(session, id=str(uuid.uuid4()), user_id=USER_2, org_id=ORG_2)
        result = await repo.a_list_active_for_org(ORG_1)
        assert len(result) == 1
        assert result[0].org_id == ORG_1

    async def test_list_by_status(self, repo, session):
        await _make_idea(session, status="emerged")
        await _make_idea(session, status="active")
        result = await repo.a_list_by_status("emerged")
        assert len(result) == 1

    async def test_list_by_status_for_org_excludes_other_orgs(self, repo, session):
        await _seed_org_user(session)
        await _make_idea(session, status="emerged", org_id=ORG_1)
        await _make_idea(session, id=str(uuid.uuid4()), user_id=USER_2, org_id=ORG_2, status="emerged")
        result = await repo.a_list_by_status_for_org("emerged", ORG_1)
        assert len(result) == 1
        assert result[0].org_id == ORG_1

    async def test_update_status(self, repo, session):
        idea = await _make_idea(session, status="emerged")
        updated = await repo.a_update_status(idea.id, "active", trigger="test")
        assert updated.status == "active"

    async def test_archive(self, repo, session):
        idea = await _make_idea(session)
        assert idea.archived_at is None
        await repo.a_archive(idea.id)
        assert idea.archived_at is not None

    async def test_hard_delete_archived_for_org_removes_only_org_archive(self, repo, session):
        await _seed_org_user(session)
        archived = await _make_idea(session, org_id=ORG_1, archived_at=datetime.now(timezone.utc))
        child = await _make_idea(session, org_id=ORG_1, parent_id=archived.id)
        active = await _make_idea(session, org_id=ORG_1)
        other_org_archived = await _make_idea(
            session,
            id=str(uuid.uuid4()),
            user_id=USER_2,
            org_id=ORG_2,
            archived_at=datetime.now(timezone.utc),
        )
        archived_id = archived.id
        child_id = child.id
        active_id = active.id
        other_org_archived_id = other_org_archived.id

        deleted = await repo.a_hard_delete_archived_for_org(ORG_1)
        await session.flush()
        session.expire_all()

        assert deleted == 1
        assert await session.get(Idea, archived_id) is None
        assert (await session.get(Idea, child_id)).parent_id is None
        assert await session.get(Idea, active_id) is not None
        assert await session.get(Idea, other_org_archived_id) is not None


# ---------------------------------------------------------------------------
# IdeaThreadRepository
# ---------------------------------------------------------------------------

class TestIdeaThreadRepository:
    def test_model_assignment(self):
        assert IdeaThreadRepository.model is IdeaThread

    async def test_add_and_list(self, thread_repo, session):
        idea = await _make_idea(session)
        await thread_repo.a_add_message(idea.id, "user", "Hello")
        await session.flush()
        msgs = await thread_repo.a_list_by_idea(idea.id)
        assert len(msgs) == 1
        assert msgs[0].content == "Hello"


# ---------------------------------------------------------------------------
# IdeaConnectionRepository
# ---------------------------------------------------------------------------

class TestIdeaConnectionRepository:
    def test_model_assignment(self):
        assert IdeaConnectionRepository.model is IdeaConnection

    async def test_list_by_idea(self, conn_repo, session):
        a = await _make_idea(session)
        b = await _make_idea(session, title="Other")
        conn = IdeaConnection(id=str(uuid.uuid4()), source_id=a.id, target_id=b.id)
        session.add(conn)
        await session.flush()
        result = await conn_repo.a_list_by_idea(a.id)
        assert len(result) == 1

    async def test_list_all_active(self, conn_repo, session):
        a = await _make_idea(session)
        b = await _make_idea(session, title="Other")
        conn = IdeaConnection(id=str(uuid.uuid4()), source_id=a.id, target_id=b.id)
        session.add(conn)
        await session.flush()
        result = await conn_repo.a_list_all_active()
        assert len(result) >= 1

    async def test_connection_queries_scope_both_ends_to_org(self, conn_repo, session):
        await _seed_org_user(session)
        org1_a = await _make_idea(session, org_id=ORG_1)
        org1_b = await _make_idea(session, org_id=ORG_1, title="Same org")
        org2 = await _make_idea(session, id=str(uuid.uuid4()), user_id=USER_2, org_id=ORG_2)
        same_org = IdeaConnection(id=str(uuid.uuid4()), source_id=org1_a.id, target_id=org1_b.id)
        cross_org = IdeaConnection(id=str(uuid.uuid4()), source_id=org1_a.id, target_id=org2.id)
        session.add_all([same_org, cross_org])
        await session.flush()

        assert await conn_repo.a_list_all_active_for_org(ORG_1) == [same_org]
        assert await conn_repo.a_list_by_idea_for_org(org1_a.id, ORG_1) == [same_org]
        assert await conn_repo.a_get_for_org(str(same_org.id), ORG_1) == same_org
        assert await conn_repo.a_get_for_org(str(cross_org.id), ORG_1) is None
