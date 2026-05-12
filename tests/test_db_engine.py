"""Engine and session infrastructure."""
import asyncio
import inspect
import warnings
from unittest.mock import MagicMock, patch

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from brain.app.api.deps import get_db
from brain.kernel import config
from brain.platform.db import SessionFactory, engine
from brain.platform.db.legacy import get_conn, get_cursor, legacy_session_factory


def test_runtime_engine_is_asyncpg():
    """Runtime DB access uses async SQLAlchemy + asyncpg."""
    assert isinstance(engine, AsyncEngine)
    assert engine.url.drivername == "postgresql+asyncpg"


def test_runtime_session_factory_is_async():
    assert isinstance(SessionFactory, async_sessionmaker)
    session = SessionFactory()
    try:
        assert isinstance(session, AsyncSession)
    finally:
        asyncio.run(session.close())


def test_get_db_dependency_is_async():
    assert inspect.isasyncgenfunction(get_db)


def test_legacy_session_factory_is_isolated():
    assert legacy_session_factory is not SessionFactory


def test_runtime_db_module_does_not_export_raw_helpers():
    import brain.platform.db as db

    assert not hasattr(db, "get_cursor")
    assert not hasattr(db, "get_conn")


def test_database_url_helpers_keep_runtime_async_and_legacy_sync():
    assert config._to_async_pg_url("postgresql://u:p@h/db").startswith("postgresql+asyncpg://")
    assert config._to_async_pg_url("postgresql+psycopg2://u:p@h/db").startswith("postgresql+asyncpg://")
    assert config._to_sync_pg_url("postgresql+asyncpg://u:p@h/db").startswith("postgresql://")


def test_get_cursor_emits_deprecation():
    """get_cursor remains as a deprecated transitional shim."""
    mock_conn = MagicMock()
    mock_pool = MagicMock()
    mock_pool.getconn.return_value = mock_conn

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        with patch("brain.platform.db.legacy._get_pool", return_value=mock_pool):
            with get_cursor(commit=False) as cur:
                assert cur is mock_conn.cursor.return_value
        dep_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
        assert len(dep_warnings) >= 1
        assert "deprecated" in str(dep_warnings[0].message).lower()


def test_get_conn_emits_deprecation():
    """get_conn remains as a deprecated transitional shim."""
    mock_conn = MagicMock()
    mock_pool = MagicMock()
    mock_pool.getconn.return_value = mock_conn

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        with patch("brain.platform.db.legacy._get_pool", return_value=mock_pool):
            with get_conn(commit=False) as conn:
                assert conn is mock_conn
        dep_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
        assert len(dep_warnings) >= 1
        assert "deprecated" in str(dep_warnings[0].message).lower()
