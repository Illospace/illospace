"""Engine and session infrastructure."""
import inspect

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool

from brain.app.api.deps import get_db
from brain.platform.db import SessionFactory, engine
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


async def test_asyncpg_test_engine_disables_pool_reuse_across_event_loops():
    test_engine = create_async_test_engine("postgresql://user:pass@127.0.0.1/test_db")
    try:
        assert test_engine.url.drivername == "postgresql+asyncpg"
        assert isinstance(test_engine.pool, NullPool)
    finally:
        await test_engine.dispose()
