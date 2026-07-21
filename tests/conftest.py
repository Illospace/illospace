"""Shared fixtures for illo-brain tests."""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text
from sqlalchemy.schema import CreateTable

from tests.db_engine_utils import create_async_test_engine

# Ensure the repository package is importable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))


@pytest.fixture
def mock_cursor():
    """A mock RealDictCursor that tracks execute calls."""
    cur = MagicMock()
    cur.fetchone.return_value = {"id": 1, "cnt": 0, "c": 0, "total": 5}
    cur.fetchall.return_value = []
    cur.rowcount = 0
    return cur


@pytest.fixture
def mock_db(mock_cursor):
    """Return a DB-shaped mock cursor for tests that do not touch a real DB."""

    yield mock_cursor


@pytest.fixture
def mock_uow():
    """A mock UnitOfWork for tests that need to mock DB operations.

    Usage:
        def test_something(mock_uow):
            mock_uow.session.execute.return_value.mappings.return_value.all.return_value = [{"id": 1}]
            mock_uow.session.get.return_value = SomeMockObj()
            with patch("brain.some_module.UnitOfWork", return_value=mock_uow):
                result = function_under_test()
    """
    uow = MagicMock()
    uow.__enter__ = MagicMock(return_value=uow)
    uow.__exit__ = MagicMock(return_value=False)
    # Default empty results
    uow.session.execute.return_value.mappings.return_value.all.return_value = []
    uow.session.execute.return_value.mappings.return_value.first.return_value = None
    uow.session.execute.return_value.scalar_one.return_value = 1
    uow.session.execute.return_value.scalar_one_or_none.return_value = None
    uow.session.execute.return_value.scalars.return_value.all.return_value = []
    return uow


@pytest.fixture
def mock_embeddings():
    """Patch embedding functions to return deterministic vectors."""
    import numpy as np
    fake_vec = np.zeros(2000, dtype=np.float32)
    fake_vec[0] = 1.0

    patches = [
        patch("brain.systems.memory.embeddings.embed_document", return_value=fake_vec),
        patch("brain.systems.memory.embeddings.embed_query", return_value=fake_vec),
        patch("brain.systems.memory.embeddings.embed_batch", return_value=[fake_vec]),
    ]
    for p in patches:
        p.start()
    yield fake_vec
    for p in patches:
        p.stop()


@pytest.fixture
async def rollback_cursor(db_engine):
    """Yield an async cursor-like object inside a transaction that always rolls back.

    Use for integration tests that need a real DB but must not pollute it.
    The entire transaction is rolled back after the test, leaving zero residue.
    """

    connection = await db_engine.connect()
    transaction = await connection.begin()
    cur = _RollbackCursor(connection)
    try:
        yield cur
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.fixture
def rollback_db(rollback_cursor):
    """Alias for DB-backed tests that need rollback-scoped SQL access."""

    yield rollback_cursor


class _RollbackCursor:
    def __init__(self, connection):
        self._connection = connection
        self._result = None
        self.rowcount = -1

    async def execute(self, statement: str, params: dict | None = None):
        self._result = await self._connection.execute(text(statement), params or {})
        self.rowcount = self._result.rowcount
        return self

    async def fetchone(self):
        assert self._result is not None
        row = self._result.mappings().first()
        return dict(row) if row else None

    async def fetchall(self):
        assert self._result is not None
        return [dict(row) for row in self._result.mappings().all()]


@pytest.fixture(scope="session")
async def db_engine():
    """Async SQLAlchemy engine connected to Docker test Postgres. Session-scoped."""
    if not TEST_DB_URL:
        pytest.skip("TEST_DB_URL not set")
    engine = create_async_test_engine(TEST_DB_URL)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def tmp_workspace(tmp_path):
    """Create a temporary workspace structure."""
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "2026-03-01.md").write_text(
        "# Daily Log\n\n## Bug Fix\nFixed a critical bug in the provider API.\n\n"
        "## Lesson Learned\nAlways verify data assumptions before shipping.\n"
    )
    return tmp_path


@pytest.fixture(autouse=True)
def allow_asgi_test_host_for_dev_auth_fallback(monkeypatch):
    """Let ASGI test clients use the local-dev auth fallback.

    Production code only trusts loopback hosts for the fallback. Many API tests
    use http://test or TestClient's http://testserver synthetic host, so widen
    the local host set inside pytest instead of weakening runtime auth checks.
    """
    from brain.app.api import auth

    monkeypatch.setattr(
        auth,
        "_LOCALHOST_NAMES",
        auth._LOCALHOST_NAMES | {"test", "testserver"},
    )


@pytest.fixture
def sqlite_postgres_ddl_patch():
    """Make postgres-typed tables (JSONB, BIGINT, ::jsonb defaults) render on
    SQLite so repository tests can create them. Same guarded, idempotent
    patch as test_agent_run_state_machine._patch_sqlite_for_agent_run_tables
    (shared here for tests outside that module)."""
    import re

    from sqlalchemy.dialects.sqlite.base import SQLiteDDLCompiler, SQLiteTypeCompiler

    if not hasattr(SQLiteTypeCompiler, "visit_JSONB"):
        SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "TEXT"
    SQLiteTypeCompiler.visit_BIGINT = lambda self, type_, **kw: "INTEGER"
    original = SQLiteDDLCompiler.get_column_default_string
    if getattr(original, "_agent_run_patch", False):
        return

    def patched(self, column, **kw):
        result = original(self, column, **kw)
        if result:
            result = re.sub(r"::jsonb", "", result)
            result = result.replace("NOW()", "CURRENT_TIMESTAMP")
        return result

    patched._agent_run_patch = True
    SQLiteDDLCompiler.get_column_default_string = patched


# --- Docker test DB fixtures ---

TEST_DB_URL = os.environ.get("TEST_DB_URL")


requires_db = pytest.mark.requires_db


def pytest_collection_modifyitems(config, items):
    if TEST_DB_URL is not None:
        return
    skip_db = pytest.mark.skip(reason="TEST_DB_URL not set — run in the Postgres CI lane")
    for item in items:
        if "requires_db" in item.keywords:
            item.add_marker(skip_db)


@pytest.fixture
async def async_sqlite_session_factory():
    """Create isolated async SQLite sessions for repository unit tests."""

    pytest.importorskip("aiosqlite")

    from sqlalchemy import event
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    sessions = []
    engines = []

    async def make_session(tables, *, connect_listener=None, enable_foreign_keys: bool = False):
        engine = create_async_engine("sqlite+aiosqlite://", echo=False)
        engines.append(engine)
        if connect_listener is not None:
            event.listen(engine.sync_engine, "connect", connect_listener)
        async with engine.begin() as connection:
            for table in tables:
                await connection.execute(CreateTable(table, if_not_exists=True))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        session = factory()
        if enable_foreign_keys:
            await session.execute(text("PRAGMA foreign_keys=ON"))
        sessions.append(session)
        return session

    try:
        yield make_session
    finally:
        for session in reversed(sessions):
            await session.close()
        for engine in reversed(engines):
            await engine.dispose()


@pytest.fixture
async def db_session(db_engine):
    """Per-test async ORM session that rolls back after each test. Zero residue.

    Use for SQLAlchemy ORM tests. Do NOT mix with rollback_db in the same test.
    """
    from sqlalchemy.ext.asyncio import AsyncSession

    connection = await db_engine.connect()
    transaction = await connection.begin()
    session = AsyncSession(bind=connection, expire_on_commit=False)
    try:
        yield session
    finally:
        await session.close()
        await transaction.rollback()
        await connection.close()


@pytest.fixture
def unit_of_work_for_session(db_session):
    """Patch target for UnitOfWork-backed code that should share db_session.

    Many older DB tests seed rows inside a rollback transaction. Production code
    now uses UnitOfWork, so tests that need read-your-writes behavior can patch
    the module-local UnitOfWork symbol to this lightweight transaction wrapper.
    """
    from brain.platform.db.repositories.unit_of_work import UnitOfWork as AsyncUnitOfWork

    class _SessionUnitOfWork(AsyncUnitOfWork):
        async def __aenter__(self):
            self._async_session = db_session
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            if exc_type is None:
                await db_session.flush()
            self._async_session = None
            self._clear_cached_repositories()
            return False

    return _SessionUnitOfWork
