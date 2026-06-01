"""Illo Brain async database layer."""
from __future__ import annotations

import os
from collections.abc import Mapping

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from brain.kernel import config

_ENABLED_VALUES = {"1", "true", "yes", "on"}


def _env_enabled(values: Mapping[str, str], key: str) -> bool:
    return values.get(key, "").strip().lower() in _ENABLED_VALUES


def _uses_inline_multiloop_runner(env: Mapping[str, str] | None = None) -> bool:
    values = env if env is not None else os.environ
    explicit_runner = _env_enabled(values, "CORTEX_INLINE_RUNNER")
    explicit_dispatcher = _env_enabled(values, "CORTEX_INLINE_DISPATCHER")
    explicit_nullpool = _env_enabled(values, "ILLO_DB_NULLPOOL")
    worker_cycle_scheduler = (
        _env_enabled(values, "ILLO_WORKER_ENABLE_CYCLE_SCHEDULER")
        and not _env_enabled(values, "ILLO_WORKER_DISABLE_CYCLE_SCHEDULER")
    )
    return (
        explicit_runner
        or explicit_dispatcher
        or explicit_nullpool
        or worker_cycle_scheduler
    )


def _engine_kwargs_for_environment(env: Mapping[str, str] | None = None) -> dict:
    kwargs = {
        "pool_pre_ping": True,
        "echo": False,
    }
    if _uses_inline_multiloop_runner(env):
        # asyncpg connections are bound to the event loop that created them.
        # The inline Cortex runner uses its own loop in a supervisor thread, so
        # pooled connections cannot safely be shared with the API loop.
        kwargs["poolclass"] = NullPool
        return kwargs
    kwargs["pool_size"] = config.DB_POOL_MAX
    kwargs["max_overflow"] = config.DB_POOL_OVERFLOW
    kwargs["pool_timeout"] = config.DB_POOL_TIMEOUT_SECONDS
    return kwargs


engine = create_async_engine(config.DB_URL, **_engine_kwargs_for_environment())

SessionFactory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

AsyncSessionFactory = SessionFactory

__all__ = [
    "AsyncSessionFactory",
    "SessionFactory",
    "engine",
]
