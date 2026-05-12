"""Illo Brain async database layer."""
from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from brain.kernel import config

engine = create_async_engine(
    config.DB_URL,
    pool_size=config.DB_POOL_MAX,
    pool_pre_ping=True,
    echo=False,
)

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
