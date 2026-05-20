"""Org vault repositories."""
from __future__ import annotations

from typing import Sequence

from sqlalchemy import select

from brain.platform.db.models.vault import Secret, VaultAccessLog, VaultMissingRequest
from brain.platform.db.repositories.base import BaseRepository


class VaultRepository(BaseRepository[Secret]):
    model = Secret

    async def a_list_by_org(self, org_id: str) -> Sequence[Secret]:
        stmt = (
            select(Secret)
            .where(Secret.org_id == org_id)
            .order_by(Secret.category, Secret.key_name)
        )
        return (await self._session.scalars(stmt)).all()

    async def a_list_by_org_and_category(
        self, org_id: str, category: str
    ) -> Sequence[Secret]:
        stmt = (
            select(Secret)
            .where(Secret.org_id == org_id, Secret.category == category)
            .order_by(Secret.key_name)
        )
        return (await self._session.scalars(stmt)).all()

    async def a_get_by_key(self, org_id: str, key_name: str) -> Secret | None:
        result = await self._session.scalars(
            select(Secret)
            .where(Secret.org_id == org_id, Secret.key_name == key_name)
            .limit(1)
        )
        return result.first()

    async def list_missing_requests(
        self,
        *,
        org_id: str,
    ) -> Sequence[VaultMissingRequest]:
        stmt = (
            select(VaultMissingRequest)
            .where(
                VaultMissingRequest.org_id == org_id,
                VaultMissingRequest.resolved == False,  # noqa: E712
            )
            .order_by(VaultMissingRequest.last_requested.desc())
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
        stmt = (
            select(VaultAccessLog)
            .where(VaultAccessLog.org_id == org_id)
            .order_by(VaultAccessLog.accessed_at.desc())
            .limit(limit)
        )
        return (await self._session.scalars(stmt)).all()

    async def a_log_access(
        self,
        *,
        org_id: str,
        actor_user_id: str | None,
        secret_id: int,
        key_name: str,
        action: str,
    ) -> VaultAccessLog:
        entry = VaultAccessLog(
            org_id=org_id,
            actor_user_id=actor_user_id,
            secret_id=secret_id,
            key_name=key_name,
            action=action,
        )
        self._session.add(entry)
        return entry
