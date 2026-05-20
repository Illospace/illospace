"""VaultRepository — domain queries for secrets, shares, access log."""
from __future__ import annotations

from typing import Sequence

from sqlalchemy import and_, or_, select

from brain.platform.db.models.vault import Secret, VaultAccessLog, VaultMissingRequest, VaultShare
from brain.platform.db.repositories.base import BaseRepository


class VaultRepository(BaseRepository[Secret]):
    model = Secret

    async def a_list_by_user(self, user_id: str) -> Sequence[Secret]:
        stmt = (
            select(Secret)
            .where(Secret.user_id == user_id)
            .order_by(Secret.category, Secret.key_name)
        )
        return (await self._session.scalars(stmt)).all()

    async def a_list_by_user_and_category(
        self, user_id: str, category: str
    ) -> Sequence[Secret]:
        stmt = (
            select(Secret)
            .where(Secret.user_id == user_id, Secret.category == category)
            .order_by(Secret.key_name)
        )
        return (await self._session.scalars(stmt)).all()

    async def a_get_by_key(
        self,
        user_id: str,
        key_name: str,
        *,
        org_id: str | None = None,
    ) -> Secret | None:
        if org_id:
            org_stmt = (
                select(Secret)
                .where(Secret.org_id == org_id, Secret.key_name == key_name)
                .order_by(Secret.id.desc())
                .limit(1)
            )
            org_result = await self._session.scalars(org_stmt)
            if secret := org_result.first():
                return secret

        stmt = select(Secret).where(
            Secret.user_id == user_id, Secret.key_name == key_name
        )
        if org_id:
            stmt = stmt.where(Secret.org_id.is_(None))
        result = await self._session.scalars(stmt)
        return result.first()

    async def list_missing_requests(
        self,
        *,
        user_id: str | None = None,
        org_id: str | None = None,
    ) -> Sequence[VaultMissingRequest]:
        stmt = (
            select(VaultMissingRequest)
            .where(VaultMissingRequest.resolved == False)  # noqa: E712
        )
        if org_id:
            stmt = stmt.where(VaultMissingRequest.org_id == org_id)
        elif user_id:
            stmt = stmt.where(VaultMissingRequest.user_id == user_id)
        else:
            return []
        stmt = stmt.order_by(VaultMissingRequest.last_requested.desc())
        return (await self._session.scalars(stmt)).all()

    async def revoke_share(self, share_id: int, user_id: str) -> bool:
        """Revoke a share by id, only if the current user owns the secret."""
        from datetime import datetime, timezone

        stmt = select(VaultShare).where(VaultShare.id == share_id)
        share = (await self._session.scalars(stmt)).first()
        if not share:
            return False
        # Verify the user owns the underlying secret
        secret = await self.a_get(share.secret_id)
        if not secret or secret.user_id != user_id:
            return False
        share.revoked_at = datetime.now(timezone.utc)
        await self._session.flush()
        return True


class VaultShareRepository(BaseRepository[VaultShare]):
    model = VaultShare

    async def a_list_by_secret(self, secret_id: int) -> Sequence[VaultShare]:
        stmt = select(VaultShare).where(
            VaultShare.secret_id == secret_id,
            VaultShare.revoked_at.is_(None),
        )
        return (await self._session.scalars(stmt)).all()

    async def a_list_shared_with_user(self, user_id: str) -> Sequence[VaultShare]:
        stmt = select(VaultShare).where(
            VaultShare.shared_with_user_id == user_id,
            VaultShare.revoked_at.is_(None),
        )
        return (await self._session.scalars(stmt)).all()


class VaultAccessLogRepository(BaseRepository[VaultAccessLog]):
    model = VaultAccessLog

    async def a_list_recent(self, *, limit: int = 100) -> Sequence[VaultAccessLog]:
        stmt = (
            select(VaultAccessLog)
            .order_by(VaultAccessLog.accessed_at.desc())
            .limit(limit)
        )
        return (await self._session.scalars(stmt)).all()

    async def list_recent_for_org(self, org_id: str, *, limit: int = 100) -> Sequence[VaultAccessLog]:
        from brain.platform.db.models.org import User

        stmt = (
            select(VaultAccessLog)
            .outerjoin(User, User.id == VaultAccessLog.user_id)
            .where(
                or_(
                    VaultAccessLog.org_id == org_id,
                    and_(VaultAccessLog.org_id.is_(None), User.org_id == org_id),
                )
            )
            .order_by(VaultAccessLog.accessed_at.desc())
            .limit(limit)
        )
        return (await self._session.scalars(stmt)).all()

    async def a_log_access(
        self,
        user_id: str,
        secret_id: int,
        key_name: str,
        action: str,
    ) -> VaultAccessLog:
        entry = VaultAccessLog(
            user_id=user_id,
            secret_id=secret_id,
            key_name=key_name,
            action=action,
        )
        self._session.add(entry)
        return entry
