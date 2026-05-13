"""Engine and session infrastructure."""
import inspect

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from brain.app.api.deps import get_db
from brain.kernel import config
from brain.platform.db import SessionFactory, engine


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
