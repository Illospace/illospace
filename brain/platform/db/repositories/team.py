"""Team repositories — domain queries for users, orgs, and org API keys."""
from __future__ import annotations

from typing import Sequence

from sqlalchemy import select

from brain.platform.db.models.org import Org, OrgApiKey, User
from brain.platform.db.repositories.base import BaseRepository


class OrgRepository(BaseRepository[Org]):
    model = Org
    pk_column = "id"

    async def get_first(self) -> Org | None:
        stmt = select(Org).order_by(Org.created_at).limit(1)
        return (await self._session.scalars(stmt)).first()


class TeamRepository(BaseRepository[User]):
    model = User

    async def a_list_by_org(self, org_id: str) -> Sequence[User]:
        stmt = (
            select(User)
            .where(User.org_id == org_id)
            .order_by(User.name)
        )
        return (await self._session.scalars(stmt)).all()

    async def get_by_email(self, email: str) -> User | None:
        stmt = select(User).where(User.email == email)
        return (await self._session.scalars(stmt)).first()

    async def a_get_by_id(self, user_id: str) -> User | None:
        return await self._session.get(User, user_id)

    async def list_approved(self, org_id: str) -> Sequence[User]:
        stmt = (
            select(User)
            .where(User.org_id == org_id, User.approved == True)  # noqa: E712
            .order_by(User.name)
        )
        return (await self._session.scalars(stmt)).all()

    async def list_pending(self, org_id: str) -> Sequence[User]:
        stmt = (
            select(User)
            .where(User.org_id == org_id, User.approved == False)  # noqa: E712
            .order_by(User.created_at)
        )
        return (await self._session.scalars(stmt)).all()

    async def list_all(self, *, limit: int | None = None) -> Sequence[User]:
        stmt = select(User).order_by(User.created_at)
        if limit:
            stmt = stmt.limit(limit)
        return (await self._session.scalars(stmt)).all()

    async def has_any(self) -> bool:
        stmt = select(User.id).limit(1)
        return (await self._session.scalars(stmt)).first() is not None


class OrgApiKeyRepository(BaseRepository[OrgApiKey]):
    model = OrgApiKey

    async def list_by_org(self, org_id: str) -> Sequence[OrgApiKey]:
        stmt = (
            select(OrgApiKey)
            .where(OrgApiKey.org_id == org_id)
            .order_by(OrgApiKey.created_at.desc())
        )
        return (await self._session.scalars(stmt)).all()
