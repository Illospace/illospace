"""Base repository and UnitOfWork tests (no DB needed — uses in-memory SQLite)."""
import pytest
from sqlalchemy import create_engine, String
from sqlalchemy.orm import Session, Mapped, mapped_column

from brain.platform.db.base import Base
from brain.platform.db.repositories.base import BaseRepository


class _Item(Base):
    __tablename__ = "_test_item"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))


class _ItemRepo(BaseRepository["_Item"]):
    model = _Item


def _make_session():
    """In-memory SQLite — only creates the test table, not all models."""
    eng = create_engine("sqlite://", echo=False)
    _Item.__table__.create(eng)
    return Session(eng)


def test_create_and_get():
    session = _make_session()
    repo = _ItemRepo(session)
    item = repo.create(name="alpha")
    session.flush()
    assert item.id is not None
    found = repo.get(item.id)
    assert found is not None
    assert found.name == "alpha"


def test_get_or_raise_missing():
    session = _make_session()
    repo = _ItemRepo(session)
    with pytest.raises(LookupError, match="not found"):
        repo.get_or_raise(9999)


def test_list_all_ordered():
    session = _make_session()
    repo = _ItemRepo(session)
    repo.create(name="beta")
    repo.create(name="alpha")
    session.flush()
    items = repo.list_all()
    assert len(items) == 2
    assert items[0].id < items[1].id


def test_list_all_with_limit():
    session = _make_session()
    repo = _ItemRepo(session)
    for i in range(5):
        repo.create(name=f"item-{i}")
    session.flush()
    items = repo.list_all(limit=3)
    assert len(items) == 3


def test_delete():
    session = _make_session()
    repo = _ItemRepo(session)
    item = repo.create(name="doomed")
    session.flush()
    repo.delete(item)
    session.flush()
    assert repo.get(item.id) is None
