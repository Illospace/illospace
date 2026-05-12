"""IdeaRepository tests using in-memory SQLite."""
import uuid
from datetime import datetime, timezone

import re

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler, SQLiteDDLCompiler
from sqlalchemy.orm import Session

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
def engine():
    _patch_sqlite_for_pg_types()
    _register_sqlite_adapters()
    eng = create_engine("sqlite://", echo=False)
    event.listen(eng, "connect", _register_sqlite_functions)
    Org.__table__.create(eng, checkfirst=True)
    User.__table__.create(eng, checkfirst=True)
    Idea.__table__.create(eng, checkfirst=True)
    IdeaStateLog.__table__.create(eng, checkfirst=True)
    IdeaConnection.__table__.create(eng, checkfirst=True)
    IdeaThread.__table__.create(eng, checkfirst=True)
    return eng


@pytest.fixture
def session(engine):
    s = Session(engine)
    # seed org + user for FK constraints
    org = Org(id=ORG_1, name="Test Org", slug="test-org")
    s.add(org)
    user = User(id=USER_1, org_id=ORG_1, name="Alex", email="alex@test.com")
    s.add(user)
    s.flush()
    yield s
    s.close()


@pytest.fixture
def repo(session):
    return IdeaRepository(session)


@pytest.fixture
def thread_repo(session):
    return IdeaThreadRepository(session)


@pytest.fixture
def conn_repo(session):
    return IdeaConnectionRepository(session)


def _make_idea(session, **kwargs):
    defaults = {
        "id": str(uuid.uuid4()),
        "title": "Test Idea",
        "user_id": USER_1,
    }
    defaults.update(kwargs)
    idea = Idea(**defaults)
    session.add(idea)
    session.flush()
    return idea


def _seed_org_user(session, org_id=ORG_2, user_id=USER_2):
    org = Org(id=org_id, name=f"Test {org_id}", slug=org_id)
    user = User(id=user_id, org_id=org_id, name=f"User {user_id}", email=f"{user_id}@test.com")
    session.add_all([org, user])
    session.flush()
    return org, user


# ---------------------------------------------------------------------------
# IdeaRepository
# ---------------------------------------------------------------------------

class TestIdeaRepository:
    def test_model_assignment(self):
        assert IdeaRepository.model is Idea

    def test_list_active(self, repo, session):
        _make_idea(session)
        from datetime import datetime, timezone
        _make_idea(session, archived_at=datetime.now(timezone.utc))
        result = repo.list_active()
        assert len(result) == 1

    def test_list_by_org(self, repo, session):
        _make_idea(session, org_id=ORG_1)
        _make_idea(session, org_id=None)
        result = repo.list_by_org(ORG_1)
        assert len(result) == 1

    def test_get_for_org_returns_only_matching_org(self, repo, session):
        idea = _make_idea(session, org_id=ORG_1)
        assert repo.get_for_org(idea.id, ORG_1) == idea
        assert repo.get_for_org(idea.id, ORG_2) is None

    def test_list_active_for_org_excludes_other_orgs(self, repo, session):
        _seed_org_user(session)
        _make_idea(session, org_id=ORG_1)
        _make_idea(session, id=str(uuid.uuid4()), user_id=USER_2, org_id=ORG_2)
        result = repo.list_active_for_org(ORG_1)
        assert len(result) == 1
        assert result[0].org_id == ORG_1

    def test_list_by_status(self, repo, session):
        _make_idea(session, status="emerged")
        _make_idea(session, status="active")
        result = repo.list_by_status("emerged")
        assert len(result) == 1

    def test_list_by_status_for_org_excludes_other_orgs(self, repo, session):
        _seed_org_user(session)
        _make_idea(session, status="emerged", org_id=ORG_1)
        _make_idea(session, id=str(uuid.uuid4()), user_id=USER_2, org_id=ORG_2, status="emerged")
        result = repo.list_by_status_for_org("emerged", ORG_1)
        assert len(result) == 1
        assert result[0].org_id == ORG_1

    def test_update_status(self, repo, session):
        idea = _make_idea(session, status="emerged")
        updated = repo.update_status(idea.id, "active", trigger="test")
        assert updated.status == "active"

    def test_archive(self, repo, session):
        idea = _make_idea(session)
        assert idea.archived_at is None
        repo.archive(idea.id)
        assert idea.archived_at is not None


# ---------------------------------------------------------------------------
# IdeaThreadRepository
# ---------------------------------------------------------------------------

class TestIdeaThreadRepository:
    def test_model_assignment(self):
        assert IdeaThreadRepository.model is IdeaThread

    def test_add_and_list(self, thread_repo, session):
        idea = _make_idea(session)
        thread_repo.add_message(idea.id, "user", "Hello")
        session.flush()
        msgs = thread_repo.list_by_idea(idea.id)
        assert len(msgs) == 1
        assert msgs[0].content == "Hello"


# ---------------------------------------------------------------------------
# IdeaConnectionRepository
# ---------------------------------------------------------------------------

class TestIdeaConnectionRepository:
    def test_model_assignment(self):
        assert IdeaConnectionRepository.model is IdeaConnection

    def test_list_by_idea(self, conn_repo, session):
        a = _make_idea(session)
        b = _make_idea(session, title="Other")
        conn = IdeaConnection(id=str(uuid.uuid4()), source_id=a.id, target_id=b.id)
        session.add(conn)
        session.flush()
        result = conn_repo.list_by_idea(a.id)
        assert len(result) == 1

    def test_list_all_active(self, conn_repo, session):
        a = _make_idea(session)
        b = _make_idea(session, title="Other")
        conn = IdeaConnection(id=str(uuid.uuid4()), source_id=a.id, target_id=b.id)
        session.add(conn)
        session.flush()
        result = conn_repo.list_all_active()
        assert len(result) >= 1

    def test_connection_queries_scope_both_ends_to_org(self, conn_repo, session):
        _seed_org_user(session)
        org1_a = _make_idea(session, org_id=ORG_1)
        org1_b = _make_idea(session, org_id=ORG_1, title="Same org")
        org2 = _make_idea(session, id=str(uuid.uuid4()), user_id=USER_2, org_id=ORG_2)
        same_org = IdeaConnection(id=str(uuid.uuid4()), source_id=org1_a.id, target_id=org1_b.id)
        cross_org = IdeaConnection(id=str(uuid.uuid4()), source_id=org1_a.id, target_id=org2.id)
        session.add_all([same_org, cross_org])
        session.flush()

        assert conn_repo.list_all_active_for_org(ORG_1) == [same_org]
        assert conn_repo.list_by_idea_for_org(org1_a.id, ORG_1) == [same_org]
        assert conn_repo.get_for_org(str(same_org.id), ORG_1) == same_org
        assert conn_repo.get_for_org(str(cross_org.id), ORG_1) is None
