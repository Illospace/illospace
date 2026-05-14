"""Async database helpers for tests."""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool


def async_test_db_url(url: str) -> str:
    if url.startswith("postgresql+asyncpg://"):
        return url
    if url.startswith("postgresql://"):
        return "postgresql+asyncpg://" + url.removeprefix("postgresql://")
    if url.startswith("postgres://"):
        return "postgresql+asyncpg://" + url.removeprefix("postgres://")
    if url.startswith("sqlite+aiosqlite://"):
        return url
    if url.startswith("sqlite://"):
        return "sqlite+aiosqlite://" + url.removeprefix("sqlite://")
    return url


def create_async_test_engine(url: str, **kwargs: Any) -> AsyncEngine:
    """Create an async test engine without cross-loop asyncpg pooling."""

    async_url = async_test_db_url(url)
    engine_kwargs: dict[str, Any] = {"echo": False, **kwargs}
    if async_url.startswith("postgresql+asyncpg://"):
        engine_kwargs.setdefault("poolclass", NullPool)
    return create_async_engine(async_url, **engine_kwargs)
