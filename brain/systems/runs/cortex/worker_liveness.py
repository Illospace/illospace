"""Durable liveness evidence for the standalone AgentRun worker."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.scheduler import SchedulerLivenessCheckpoint

WORKER_LIVENESS_CHECKPOINT_KEY = "agent_run_worker"


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def worker_liveness_checkpoint(session: AsyncSession) -> datetime | None:
    checkpoint = await session.get(
        SchedulerLivenessCheckpoint,
        WORKER_LIVENESS_CHECKPOINT_KEY,
    )
    return _utc(checkpoint.last_heartbeat_at) if checkpoint is not None else None


async def record_worker_liveness_checkpoint(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> datetime:
    """Advance the worker heartbeat monotonically in durable storage."""

    heartbeat_at = _utc(now or datetime.now(timezone.utc))
    if heartbeat_at is None:
        raise ValueError("now is required")
    checkpoint = await session.get(
        SchedulerLivenessCheckpoint,
        WORKER_LIVENESS_CHECKPOINT_KEY,
    )
    if checkpoint is None:
        checkpoint = SchedulerLivenessCheckpoint(
            checkpoint_key=WORKER_LIVENESS_CHECKPOINT_KEY,
            last_heartbeat_at=heartbeat_at,
            last_reconciled_at=heartbeat_at,
        )
        try:
            async with session.begin_nested():
                session.add(checkpoint)
                await session.flush()
        except IntegrityError:
            checkpoint = await session.get(
                SchedulerLivenessCheckpoint,
                WORKER_LIVENESS_CHECKPOINT_KEY,
                populate_existing=True,
            )
            if checkpoint is None:
                raise
    current = _utc(checkpoint.last_heartbeat_at)
    if current is None or current < heartbeat_at:
        checkpoint.last_heartbeat_at = heartbeat_at
    await session.commit()
    return _utc(checkpoint.last_heartbeat_at) or heartbeat_at


async def record_worker_liveness_checkpoint_async() -> datetime:
    """Record worker liveness using an independent transaction."""

    from brain.platform.db.repositories.unit_of_work import UnitOfWork

    async with UnitOfWork() as uow:
        return await record_worker_liveness_checkpoint(uow.session)


__all__ = [
    "WORKER_LIVENESS_CHECKPOINT_KEY",
    "record_worker_liveness_checkpoint",
    "record_worker_liveness_checkpoint_async",
    "worker_liveness_checkpoint",
]
