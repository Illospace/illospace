"""Generic CRUD base for all domain repositories."""
from typing import Generic, Sequence, TypeVar, Type

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

T = TypeVar("T")


class BaseRepository(Generic[T]):
    """Domain repos extend this with domain-specific queries."""

    model: Type[T]
    pk_column: str = "id"

    def __init__(self, session: Session | AsyncSession):
        self._session = session

    def get(self, id: int) -> T | None:
        return self._session.get(self.model, id)

    async def a_get(self, id: int) -> T | None:
        return await self._session.get(self.model, id)

    def get_or_raise(self, id: int) -> T:
        obj = self.get(id)
        if obj is None:
            raise LookupError(f"{self.model.__name__} {id} not found")
        return obj

    async def a_get_or_raise(self, id: int) -> T:
        obj = await self.a_get(id)
        if obj is None:
            raise LookupError(f"{self.model.__name__} {id} not found")
        return obj

    def list_all(self, *, limit: int | None = None) -> Sequence[T]:
        pk = getattr(self.model, self.pk_column)
        stmt = select(self.model).order_by(pk)
        if limit is not None:
            stmt = stmt.limit(limit)
        return self._session.scalars(stmt).all()

    async def a_list_all(self, *, limit: int | None = None) -> Sequence[T]:
        pk = getattr(self.model, self.pk_column)
        stmt = select(self.model).order_by(pk)
        if limit is not None:
            stmt = stmt.limit(limit)
        return (await self._session.scalars(stmt)).all()

    def create(self, **kwargs) -> T:
        obj = self.model(**kwargs)
        self._session.add(obj)
        return obj  # caller controls flush timing

    async def a_create(self, **kwargs) -> T:
        return self.create(**kwargs)

    def delete(self, obj: T) -> None:
        self._session.delete(obj)

    async def a_delete(self, obj: T) -> None:
        await self._session.delete(obj)
