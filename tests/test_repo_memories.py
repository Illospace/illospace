"""MemoryRepository tests using in-memory SQLite."""
import uuid
from datetime import datetime

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.orm import Session

from brain.platform.db.base import Base
from brain.platform.db.models.memory import Edge, Memory
from brain.platform.db.models.org import Org, User
from brain.platform.db.repositories.memories import EdgeRepository, MemoryRepository


def _patch_sqlite_for_pg_types():
    """Teach SQLiteTypeCompiler to handle ARRAY and Vector as TEXT."""
    if not hasattr(SQLiteTypeCompiler, "visit_ARRAY"):
        SQLiteTypeCompiler.visit_ARRAY = lambda self, type_, **kw: "TEXT"
    if not hasattr(SQLiteTypeCompiler, "visit_Vector"):
        SQLiteTypeCompiler.visit_Vector = lambda self, type_, **kw: "TEXT"
    if not hasattr(SQLiteTypeCompiler, "visit_VECTOR"):
        SQLiteTypeCompiler.visit_VECTOR = lambda self, type_, **kw: "TEXT"


def _register_sqlite_functions(dbapi_conn, connection_record):
    dbapi_conn.create_function("NOW", 0, lambda: datetime.utcnow().isoformat())
    dbapi_conn.create_function("gen_random_uuid", 0, lambda: str(uuid.uuid4()))


def _register_sqlite_adapters():
    """Allow SQLite to handle Python list/dict types by serialising to JSON."""
    import json
    import sqlite3
    sqlite3.register_adapter(list, lambda val: json.dumps(val))
    sqlite3.register_adapter(dict, lambda val: json.dumps(val))


@pytest.fixture
def engine():
    _patch_sqlite_for_pg_types()
    _register_sqlite_adapters()
    eng = create_engine("sqlite://", echo=False)
    event.listen(eng, "connect", _register_sqlite_functions)
    Org.__table__.create(eng, checkfirst=True)
    User.__table__.create(eng, checkfirst=True)
    Memory.__table__.create(eng, checkfirst=True)
    Edge.__table__.create(eng, checkfirst=True)
    return eng


_USER_UUID = str(uuid.uuid4())
_ORG_UUID = str(uuid.uuid4())


@pytest.fixture
def session(engine):
    s = Session(engine)
    org = Org(id=_ORG_UUID, name="Test Org", slug="test-org")
    s.add(org)
    user = User(id=_USER_UUID, org_id=_ORG_UUID, name="Alex", email="alex@test.com")
    s.add(user)
    s.flush()
    yield s
    s.close()


@pytest.fixture
def repo(session):
    return MemoryRepository(session)


@pytest.fixture
def edge_repo(session):
    return EdgeRepository(session)


def _make_memory(session, **kwargs):
    defaults = {
        "content": "test memory content",
        "memory_type": "episodic",
        "user_id": _USER_UUID,
        "salience": 5.0,
        "access_count": 0,
        "tags": None,
        "source_memory_ids": None,
    }
    defaults.update(kwargs)
    m = Memory(**defaults)
    session.add(m)
    session.flush()
    return m


class TestMemoryRepository:
    def test_model_assignment(self):
        assert MemoryRepository.model is Memory

    def test_list_active(self, repo, session):
        _make_memory(session, archived=False)
        _make_memory(session, archived=True)
        result = repo.list_active()
        assert len(result) == 1

    def test_list_by_type(self, repo, session):
        _make_memory(session, memory_type="episodic")
        _make_memory(session, memory_type="semantic")
        result = repo.list_by_type("episodic")
        assert len(result) == 1

    def test_search(self, repo, session):
        _make_memory(session, content="SQLAlchemy is great")
        _make_memory(session, content="Nothing special")
        result = repo.search("SQLAlchemy")
        assert len(result) == 1

    def test_get_graph_data(self, repo, session):
        m1 = _make_memory(session, content="node 1")
        m2 = _make_memory(session, content="node 2")
        edge = Edge(source_id=m1.id, target_id=m2.id, relationship="related")
        session.add(edge)
        session.flush()
        data = repo.get_graph_data()
        assert len(data["nodes"]) == 2
        assert len(data["edges"]) == 1


class TestEdgeRepository:
    def test_model_assignment(self):
        assert EdgeRepository.model is Edge

    def test_list_by_memory(self, edge_repo, session):
        m1 = _make_memory(session, content="a")
        m2 = _make_memory(session, content="b")
        edge = Edge(source_id=m1.id, target_id=m2.id, relationship="related")
        session.add(edge)
        session.flush()
        result = edge_repo.list_by_memory(m1.id)
        assert len(result) == 1

    def test_neighborhood(self, edge_repo, session):
        m1 = _make_memory(session, content="a")
        m2 = _make_memory(session, content="b")
        edge = Edge(source_id=m1.id, target_id=m2.id, relationship="related")
        session.add(edge)
        session.flush()
        result = edge_repo.neighborhood(m1.id)
        assert len(result) == 1
