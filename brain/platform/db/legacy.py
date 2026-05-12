"""Explicit compatibility layer for synchronous database access.

New runtime code should use ``brain.platform.db.engine``, ``SessionFactory``,
``get_db``, and ``async with UnitOfWork()``. This module exists for Alembic,
older CLIs, and tests that have not yet moved to ``AsyncSession``.
"""
from __future__ import annotations

import warnings
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from brain.kernel import config

legacy_engine = create_engine(
    config.DB_SYNC_URL,
    pool_size=config.DB_POOL_MAX,
    pool_pre_ping=True,
    echo=False,
)

legacy_session_factory = sessionmaker(bind=legacy_engine, expire_on_commit=False)

_pool: ThreadedConnectionPool | None = None


def _get_pool() -> ThreadedConnectionPool:
    global _pool
    if _pool is None:
        _pool = ThreadedConnectionPool(
            config.DB_POOL_MIN,
            config.DB_POOL_MAX,
            **config.DB_DSN,
        )
    return _pool


def close_pool() -> None:
    """Shut down the legacy psycopg2 pool."""

    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None


@contextmanager
def _cursor_from_pool(pool_getter, commit: bool = True):
    warnings.warn(
        "get_cursor is deprecated, use async UnitOfWork/repositories instead",
        DeprecationWarning,
        stacklevel=2,
    )
    pool = pool_getter()
    conn = pool.getconn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        yield cur
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


@contextmanager
def get_cursor(commit: bool = True):
    """Yield a RealDictCursor from the deprecated psycopg2 pool."""

    with _cursor_from_pool(_get_pool, commit=commit) as cur:
        yield cur


@contextmanager
def _conn_from_pool(pool_getter, commit: bool = True):
    warnings.warn(
        "get_conn is deprecated, use async UnitOfWork/repositories instead",
        DeprecationWarning,
        stacklevel=2,
    )
    pool = pool_getter()
    conn = pool.getconn()
    try:
        yield conn
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


@contextmanager
def get_conn(commit: bool = True):
    """Yield a raw connection from the deprecated psycopg2 pool."""

    with _conn_from_pool(_get_pool, commit=commit) as conn:
        yield conn
