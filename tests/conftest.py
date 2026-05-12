"""Shared fixtures for illo-brain tests."""

import os
import sys
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

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
    """Patch db.get_cursor to yield mock_cursor."""
    @contextmanager
    def _get_cursor(commit=True):
        yield mock_cursor

    with patch("brain.platform.db.get_cursor", _get_cursor), \
         patch("brain.platform.db._get_pool"):
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
def rollback_cursor():
    """Yields a real DB cursor inside a transaction that always rolls back.

    Use for integration tests that need a real DB but must not pollute it.
    The entire transaction is rolled back after the test, leaving zero residue.
    """
    import psycopg2.extras as _extras
    import brain.platform.db as db
    conn = db._get_pool().getconn()
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=_extras.RealDictCursor)
    try:
        yield cur
    finally:
        conn.rollback()
        db._get_pool().putconn(conn)


@pytest.fixture
def rollback_db():
    """Patches db.get_cursor globally so ALL code paths use a rollback transaction.

    Unlike rollback_cursor (which passes a cursor directly), this fixture
    monkey-patches core.db.get_cursor so that production code calling
    get_cursor() internally still goes through the same rolled-back transaction.

    Use for integration tests where the code under test calls get_cursor() itself.
    """
    import psycopg2.extras as _extras
    import brain.platform.db as db

    conn = db._get_pool().getconn()
    conn.autocommit = False
    cur = conn.cursor(cursor_factory=_extras.RealDictCursor)

    # Create a savepoint so nested get_cursor calls don't commit
    cur.execute("SAVEPOINT rollback_db_outer")

    @contextmanager
    def _fake_get_cursor(commit=True):
        # Nested savepoint so each get_cursor() call is isolated
        cur.execute("SAVEPOINT nested_call")
        try:
            yield cur
            if commit:
                cur.execute("RELEASE SAVEPOINT nested_call")
        except Exception:
            cur.execute("ROLLBACK TO SAVEPOINT nested_call")
            raise

    original_get_cursor = db.get_cursor
    db.get_cursor = _fake_get_cursor
    try:
        yield cur
    finally:
        db.get_cursor = original_get_cursor
        conn.rollback()
        db._get_pool().putconn(conn)


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


# --- Docker test DB fixtures ---

TEST_DB_URL = os.environ.get("TEST_DB_URL")

requires_db = pytest.mark.skipif(
    not TEST_DB_URL,
    reason="TEST_DB_URL not set — run via scripts/test-with-db.sh",
)


@pytest.fixture(scope="session")
def db_engine():
    """SQLAlchemy engine connected to Docker test Postgres. Session-scoped."""
    from sqlalchemy import create_engine

    if not TEST_DB_URL:
        pytest.skip("TEST_DB_URL not set")
    engine = create_engine(TEST_DB_URL, echo=False)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(db_engine):
    """Per-test ORM session that rolls back after each test. Zero residue.

    Use for SQLAlchemy ORM tests. Do NOT mix with rollback_db in the same test.
    """
    from sqlalchemy.orm import sessionmaker

    connection = db_engine.connect()
    transaction = connection.begin()
    session = sessionmaker(bind=connection, expire_on_commit=False)()
    yield session
    session.close()
    transaction.rollback()
    connection.close()


@pytest.fixture
def unit_of_work_for_session(db_session):
    """Patch target for UnitOfWork-backed code that should share db_session.

    Many older DB tests seed rows inside a rollback transaction. Production code
    now uses UnitOfWork, so tests that need read-your-writes behavior can patch
    the module-local UnitOfWork symbol to this lightweight transaction wrapper.
    """

    class _SessionUnitOfWork:
        def __enter__(self):
            self.session = db_session
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            if exc_type is None:
                db_session.flush()
            return False

    return _SessionUnitOfWork
