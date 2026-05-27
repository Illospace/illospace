"""Engine and session infrastructure."""
import inspect

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool

from brain.app.api.deps import get_db
from brain.kernel import config
from brain.platform.db import SessionFactory, _engine_kwargs_for_environment, engine
from tests.db_engine_utils import create_async_test_engine


def test_runtime_engine_is_asyncpg():
    """Runtime DB access uses async SQLAlchemy + asyncpg."""
    assert isinstance(engine, AsyncEngine)
    assert engine.url.drivername == "postgresql+asyncpg"


async def test_runtime_session_factory_is_async():
    assert isinstance(SessionFactory, async_sessionmaker)
    session = SessionFactory()
    try:
        assert isinstance(session, AsyncSession)
    finally:
        await session.close()


def test_get_db_dependency_is_async():
    assert inspect.isasyncgenfunction(get_db)


def test_runtime_db_module_does_not_export_raw_helpers():
    import brain.platform.db as db

    assert not hasattr(db, "get_cursor")
    assert not hasattr(db, "get_conn")
    assert not hasattr(db, "legacy_session_factory")


def test_database_url_helpers_keep_runtime_async():
    assert config._to_async_pg_url("postgresql://u:p@h/db").startswith("postgresql+asyncpg://")
    assert config._to_async_pg_url("postgresql+psycopg2://u:p@h/db").startswith("postgresql+asyncpg://")
    assert not hasattr(config, "DB_SYNC_URL")


async def test_asyncpg_test_engine_disables_pool_reuse_across_event_loops():
    test_engine = create_async_test_engine("postgresql://user:pass@127.0.0.1/test_db")
    try:
        assert test_engine.url.drivername == "postgresql+asyncpg"
        assert isinstance(test_engine.pool, NullPool)
    finally:
        await test_engine.dispose()


def test_runtime_engine_disables_pool_reuse_for_inline_runner():
    kwargs = _engine_kwargs_for_environment({"CORTEX_INLINE_RUNNER": "1"})

    assert kwargs["poolclass"] is NullPool
    assert "pool_size" not in kwargs


def test_runtime_engine_disables_pool_reuse_for_legacy_inline_dispatcher():
    kwargs = _engine_kwargs_for_environment({"CORTEX_INLINE_DISPATCHER": "true"})

    assert kwargs["poolclass"] is NullPool
    assert "pool_size" not in kwargs


def test_runtime_engine_keeps_pool_for_single_loop_runtime():
    kwargs = _engine_kwargs_for_environment({})

    assert kwargs["pool_size"] == config.DB_POOL_MAX
    assert kwargs["max_overflow"] == config.DB_POOL_OVERFLOW
    assert kwargs["pool_timeout"] == config.DB_POOL_TIMEOUT_SECONDS
    assert "poolclass" not in kwargs
