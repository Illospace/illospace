"""Illo Brain database layer.

SQLAlchemy ``engine`` and ``SessionFactory`` are the runtime standard. The
psycopg2 helpers below remain only as deprecated transitional shims for older
tests and scripts while runtime modules move to repositories/UnitOfWork.
"""

import warnings
from contextlib import contextmanager

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from brain.kernel import config

# ---------------------------------------------------------------------------
# Connection pool (lazy init, thread-safe)
# ---------------------------------------------------------------------------
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
    """Shut down the pool (for clean exits)."""
    global _pool
    if _pool is not None:
        _pool.closeall()
        _pool = None


# ---------------------------------------------------------------------------
# SQLAlchemy engine + session (new standard)
# ---------------------------------------------------------------------------
engine = create_engine(
    config.DB_URL,
    pool_size=config.DB_POOL_MAX,
    pool_pre_ping=True,
    echo=False,
)

SessionFactory = sessionmaker(bind=engine, expire_on_commit=False)


# ---------------------------------------------------------------------------
# Context managers
# ---------------------------------------------------------------------------
@contextmanager
def get_cursor(commit: bool = True):
    """Yield a RealDictCursor from the legacy psycopg2 pool."""
    warnings.warn(
        "get_cursor is deprecated, use UnitOfWork or db.session",
        DeprecationWarning,
        stacklevel=2,
    )
    conn = _get_pool().getconn()
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        yield cur
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _get_pool().putconn(conn)


@contextmanager
def get_conn(commit: bool = True):
    """Yield a raw connection from the legacy psycopg2 pool."""
    warnings.warn(
        "get_conn is deprecated, use UnitOfWork or db.session",
        DeprecationWarning,
        stacklevel=2,
    )
    conn = _get_pool().getconn()
    try:
        yield conn
        if commit:
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _get_pool().putconn(conn)
