"""Base repository and UnitOfWork tests (no DB needed — uses in-memory SQLite)."""
import pytest
from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from brain.platform.db.base import Base
from brain.platform.db.repositories.base import BaseRepository


class _Item(Base):
    __tablename__ = "_test_item"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))


class _ItemRepo(BaseRepository["_Item"]):
    model = _Item


@pytest.fixture
async def session(async_sqlite_session_factory):
    return await async_sqlite_session_factory([_Item.__table__])


async def test_create_and_get(session):
    repo = _ItemRepo(session)
    item = await repo.a_create(name="alpha")
    await session.flush()
    assert item.id is not None
    found = await repo.a_get(item.id)
    assert found is not None
    assert found.name == "alpha"


async def test_get_or_raise_missing(session):
    repo = _ItemRepo(session)
    with pytest.raises(LookupError, match="not found"):
        await repo.a_get_or_raise(9999)


async def test_list_all_ordered(session):
    repo = _ItemRepo(session)
    await repo.a_create(name="beta")
    await repo.a_create(name="alpha")
    await session.flush()
    items = await repo.a_list_all()
    assert len(items) == 2
    assert items[0].id < items[1].id


async def test_list_all_with_limit(session):
    repo = _ItemRepo(session)
    for i in range(5):
        await repo.a_create(name=f"item-{i}")
    await session.flush()
    items = await repo.a_list_all(limit=3)
    assert len(items) == 3


async def test_delete(session):
    repo = _ItemRepo(session)
    item = await repo.a_create(name="doomed")
    await session.flush()
    await repo.a_delete(item)
    await session.flush()
    assert await repo.a_get(item.id) is None
