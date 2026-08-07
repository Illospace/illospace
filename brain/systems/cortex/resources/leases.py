"""Generic lease primitives for warm runtime resources."""
from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select

from brain.kernel.common.time import utcnow as _shared_utcnow

from brain.platform.events import publish_safe
from brain.platform.db.models.resource_pool import ResourceLease
from brain.platform.db.repositories.unit_of_work import UnitOfWork

logger = logging.getLogger(__name__)

_DEFAULT_LEASE_TTL_SEC = int(os.environ.get("CORTEX_RESOURCE_LEASE_TTL_SEC", "300"))


def _utcnow() -> datetime:
    return _shared_utcnow()


@dataclass(frozen=True)
class LeaseDecision:
    """Outcome of a lease acquisition attempt."""

    acquired: bool
    resource_type: str
    resource_id: str
    lease_token: str | None
    owner_run_id: int | None
    owner_worker_id: str | None
    expires_at: datetime | None
    reason: str | None = None
    lease_id: int | None = None


class ResourceLeaseManager:
    """DB-backed lease registry with conservative failure semantics."""

    def __init__(self, *, default_ttl_seconds: int = _DEFAULT_LEASE_TTL_SEC):
        self.default_ttl_seconds = max(1, int(default_ttl_seconds))

    async def acquire_lease(
        self,
        resource_type: str,
        resource_id: str,
        *,
        owner_run_id: int | None = None,
        owner_worker_id: str | None = None,
        ttl_seconds: int | None = None,
        lease_token: str | None = None,
    ) -> LeaseDecision:
        """Create a lease if the resource is currently free."""
        resource_type = (resource_type or "").strip()
        resource_id = (resource_id or "").strip()
        if not resource_type or not resource_id:
            return LeaseDecision(
                acquired=False,
                resource_type=resource_type,
                resource_id=resource_id,
                lease_token=None,
                owner_run_id=owner_run_id,
                owner_worker_id=owner_worker_id,
                expires_at=None,
                reason="resource_type and resource_id are required",
            )

        now = _utcnow()
        expires_at = now + timedelta(seconds=max(1, int(ttl_seconds or self.default_ttl_seconds)))
        token = lease_token or uuid.uuid4().hex

        try:
            async with UnitOfWork() as uow:
                active_lease = (
                    await uow.session.scalars(
                        select(ResourceLease)
                        .where(
                            ResourceLease.resource_type == resource_type,
                            ResourceLease.resource_id == resource_id,
                            ResourceLease.released_at.is_(None),
                        )
                        .order_by(ResourceLease.created_at.desc())
                    )
                ).first()
                if active_lease:
                    if active_lease.expires_at is not None and active_lease.expires_at <= now:
                        active_lease.released_at = now
                        active_lease.release_reason = "expired"
                        active_lease.heartbeat_at = now
                        publish_safe("resource_lease", {
                            "event": "reclaimed",
                            "resource_type": resource_type,
                            "resource_id": resource_id,
                            "lease_token": active_lease.lease_token,
                            "release_reason": "expired",
                            "released_at": now.isoformat(),
                        })
                    else:
                        return LeaseDecision(
                            acquired=False,
                            resource_type=resource_type,
                            resource_id=resource_id,
                            lease_token=active_lease.lease_token,
                            owner_run_id=active_lease.owner_run_id,
                            owner_worker_id=active_lease.owner_worker_id,
                            expires_at=active_lease.expires_at,
                            reason="active lease exists",
                            lease_id=active_lease.id,
                        )

                active_lease = (
                    await uow.session.scalars(
                        select(ResourceLease)
                        .where(
                            ResourceLease.resource_type == resource_type,
                            ResourceLease.resource_id == resource_id,
                            ResourceLease.released_at.is_(None),
                            ResourceLease.expires_at.isnot(None),
                            ResourceLease.expires_at > now,
                        )
                        .order_by(ResourceLease.created_at.desc())
                    )
                ).first()
                if active_lease:
                    return LeaseDecision(
                        acquired=False,
                        resource_type=resource_type,
                        resource_id=resource_id,
                        lease_token=active_lease.lease_token,
                        owner_run_id=active_lease.owner_run_id,
                        owner_worker_id=active_lease.owner_worker_id,
                        expires_at=active_lease.expires_at,
                        reason="active lease exists",
                        lease_id=active_lease.id,
                    )

                lease = ResourceLease(
                    resource_type=resource_type,
                    resource_id=resource_id,
                    owner_run_id=owner_run_id,
                    owner_worker_id=owner_worker_id,
                    lease_token=token,
                    heartbeat_at=now,
                    expires_at=expires_at,
                )
                uow.session.add(lease)
                await uow.session.flush()

                publish_safe("resource_lease", {
                    "event": "leased",
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                    "lease_token": lease.lease_token,
                    "owner_run_id": owner_run_id,
                    "owner_worker_id": owner_worker_id,
                    "expires_at": expires_at.isoformat(),
                })
                return LeaseDecision(
                    acquired=True,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    lease_token=lease.lease_token,
                    owner_run_id=owner_run_id,
                    owner_worker_id=owner_worker_id,
                    expires_at=expires_at,
                    lease_id=lease.id,
                )
        except Exception as exc:
            logger.warning("Lease acquisition failed for %s/%s: %s", resource_type, resource_id, exc)
            return LeaseDecision(
                acquired=False,
                resource_type=resource_type,
                resource_id=resource_id,
                lease_token=None,
                owner_run_id=owner_run_id,
                owner_worker_id=owner_worker_id,
                expires_at=None,
                reason=f"lease acquisition failed: {exc}",
            )

    async def release_lease(self, lease_token: str, *, release_reason: str = "released") -> bool:
        """Release an active lease by token."""
        token = (lease_token or "").strip()
        if not token:
            return False

        try:
            async with UnitOfWork() as uow:
                lease = (
                    await uow.session.scalars(
                        select(ResourceLease)
                        .where(
                            ResourceLease.lease_token == token,
                            ResourceLease.released_at.is_(None),
                        )
                        .order_by(ResourceLease.created_at.desc())
                    )
                ).first()
                if not lease:
                    return False

                now = _utcnow()
                lease.released_at = now
                lease.release_reason = release_reason
                lease.heartbeat_at = now

                publish_safe("resource_lease", {
                    "event": "released",
                    "resource_type": lease.resource_type,
                    "resource_id": lease.resource_id,
                    "lease_token": lease.lease_token,
                    "release_reason": release_reason,
                    "released_at": now.isoformat(),
                })
                return True
        except Exception as exc:
            logger.warning("Lease release failed for %s: %s", token, exc)
            return False

    async def heartbeat_lease(self, lease_token: str, *, ttl_seconds: int | None = None) -> bool:
        """Extend an active lease."""
        token = (lease_token or "").strip()
        if not token:
            return False

        try:
            async with UnitOfWork() as uow:
                lease = (
                    await uow.session.scalars(
                        select(ResourceLease)
                        .where(
                            ResourceLease.lease_token == token,
                            ResourceLease.released_at.is_(None),
                        )
                        .order_by(ResourceLease.created_at.desc())
                    )
                ).first()
                if not lease:
                    return False

                now = _utcnow()
                lease.heartbeat_at = now
                lease.expires_at = now + timedelta(seconds=max(1, int(ttl_seconds or self.default_ttl_seconds)))
                return True
        except Exception as exc:
            logger.warning("Lease heartbeat failed for %s: %s", token, exc)
            return False

    async def reclaim_expired(self, *, resource_type: str | None = None) -> int:
        """Mark expired leases as released so they can be swept safely."""
        now = _utcnow()
        reclaimed = 0

        try:
            async with UnitOfWork() as uow:
                stmt = select(ResourceLease).where(
                    ResourceLease.released_at.is_(None),
                    ResourceLease.expires_at.isnot(None),
                    ResourceLease.expires_at <= now,
                )
                if resource_type:
                    stmt = stmt.where(ResourceLease.resource_type == resource_type)

                for lease in (await uow.session.scalars(stmt)).all():
                    lease.released_at = now
                    lease.release_reason = "expired"
                    reclaimed += 1
                return reclaimed
        except Exception as exc:
            logger.warning("Expired lease reclaim failed: %s", exc)
            return reclaimed
