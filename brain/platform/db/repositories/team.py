"""Team repositories — domain queries for users, orgs, and API keys."""
from __future__ import annotations

from typing import Any, Sequence

from sqlalchemy import or_, select

from brain.platform.db.models.org import ApiKeyShare, Org, OrgApiKey, User, UserApiKey
from brain.platform.db.repositories.base import BaseRepository


class OrgRepository(BaseRepository[Org]):
    model = Org
    pk_column = "id"

    def get_first(self) -> Org | None:
        stmt = select(Org).order_by(Org.created_at).limit(1)
        return self._session.scalars(stmt).first()


class TeamRepository(BaseRepository[User]):
    model = User

    def list_by_org(self, org_id: str) -> Sequence[User]:
        stmt = (
            select(User)
            .where(User.org_id == org_id)
            .order_by(User.name)
        )
        return self._session.scalars(stmt).all()

    async def a_list_by_org(self, org_id: str) -> Sequence[User]:
        stmt = (
            select(User)
            .where(User.org_id == org_id)
            .order_by(User.name)
        )
        return (await self._session.scalars(stmt)).all()

    def get_by_email(self, email: str) -> User | None:
        stmt = select(User).where(User.email == email)
        return self._session.scalars(stmt).first()

    def get_by_id(self, user_id: str) -> User | None:
        return self._session.get(User, user_id)

    async def a_get_by_id(self, user_id: str) -> User | None:
        return await self._session.get(User, user_id)

    def list_approved(self, org_id: str) -> Sequence[User]:
        stmt = (
            select(User)
            .where(User.org_id == org_id, User.approved == True)  # noqa: E712
            .order_by(User.name)
        )
        return self._session.scalars(stmt).all()

    def list_pending(self, org_id: str) -> Sequence[User]:
        stmt = (
            select(User)
            .where(User.org_id == org_id, User.approved == False)  # noqa: E712
            .order_by(User.created_at)
        )
        return self._session.scalars(stmt).all()

    def list_all(self, *, limit: int | None = None) -> Sequence[User]:
        stmt = select(User).order_by(User.created_at)
        if limit:
            stmt = stmt.limit(limit)
        return self._session.scalars(stmt).all()

    def has_any(self) -> bool:
        stmt = select(User.id).limit(1)
        return self._session.scalars(stmt).first() is not None


class UserApiKeyRepository(BaseRepository[UserApiKey]):
    """Personal API-key access patterns shared by FastAPI and legacy shims."""

    model = UserApiKey

    def list_by_user(self, user_id: str) -> Sequence[UserApiKey]:
        stmt = (
            select(UserApiKey)
            .where(UserApiKey.user_id == user_id)
            .order_by(UserApiKey.created_at.desc())
        )
        return self._session.scalars(stmt).all()

    def list_shared_with_user(self, user_id: str) -> Sequence[dict[str, Any]]:
        stmt = (
            select(
                UserApiKey.id,
                UserApiKey.provider,
                UserApiKey.label,
                UserApiKey.is_active,
                UserApiKey.last_used_at,
                UserApiKey.total_tokens_used,
                UserApiKey.estimated_cost_usd,
                User.name.label("shared_by_name"),
                ApiKeyShare.shared_at,
            )
            .join(ApiKeyShare, ApiKeyShare.api_key_id == UserApiKey.id)
            .join(User, User.id == ApiKeyShare.shared_by_user_id)
            .where(
                ApiKeyShare.shared_with_user_id == user_id,
                ApiKeyShare.revoked_at.is_(None),
            )
        )
        return [dict(row._mapping) for row in self._session.execute(stmt).all()]

    def is_accessible_active(self, user_id: str, api_key_id: int) -> bool:
        stmt = (
            select(UserApiKey.id)
            .where(
                UserApiKey.id == api_key_id,
                UserApiKey.is_active == True,  # noqa: E712
                or_(
                    UserApiKey.user_id == user_id,
                    UserApiKey.id.in_(
                        select(ApiKeyShare.api_key_id).where(
                            ApiKeyShare.shared_with_user_id == user_id,
                            ApiKeyShare.revoked_at.is_(None),
                        )
                    ),
                ),
            )
            .limit(1)
        )
        return self._session.scalars(stmt).first() is not None

    def set_default_for_user(self, user_id: str, api_key_id: int | None) -> bool:
        user = self._session.get(User, user_id)
        if not user:
            return False
        user.default_api_key_id = api_key_id
        self._session.flush()
        return True

    def deactivate_owned(self, user_id: str, api_key_id: int) -> bool:
        stmt = select(UserApiKey).where(
            UserApiKey.id == api_key_id,
            UserApiKey.user_id == user_id,
        )
        key = self._session.scalars(stmt).first()
        if not key:
            return False
        key.is_active = False
        self._session.flush()
        return True


class OrgApiKeyRepository(BaseRepository[OrgApiKey]):
    model = OrgApiKey

    def list_by_org(self, org_id: str) -> Sequence[OrgApiKey]:
        stmt = (
            select(OrgApiKey)
            .where(OrgApiKey.org_id == org_id)
            .order_by(OrgApiKey.created_at.desc())
        )
        return self._session.scalars(stmt).all()
