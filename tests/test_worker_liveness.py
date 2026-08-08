from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from brain.platform.db.models.scheduler import SchedulerLivenessCheckpoint
from brain.systems.runs.cortex.worker_liveness import (
    WORKER_LIVENESS_CHECKPOINT_KEY,
    record_worker_liveness_checkpoint,
    worker_liveness_checkpoint,
)


@pytest.fixture
async def worker_liveness_session(
    async_sqlite_session_factory,
    sqlite_postgres_ddl_patch,
):
    return await async_sqlite_session_factory([SchedulerLivenessCheckpoint.__table__])


async def test_worker_liveness_checkpoint_is_created_and_advances_monotonically(
    worker_liveness_session,
):
    first = datetime(2026, 8, 8, 20, 0, tzinfo=timezone.utc)
    second = first + timedelta(seconds=5)

    assert await worker_liveness_checkpoint(worker_liveness_session) is None
    assert (
        await record_worker_liveness_checkpoint(
            worker_liveness_session,
            now=first,
        )
        == first
    )
    assert (
        await record_worker_liveness_checkpoint(
            worker_liveness_session,
            now=second,
        )
        == second
    )
    assert (
        await record_worker_liveness_checkpoint(
            worker_liveness_session,
            now=first,
        )
        == second
    )

    stored = await worker_liveness_session.get(
        SchedulerLivenessCheckpoint,
        WORKER_LIVENESS_CHECKPOINT_KEY,
    )
    assert stored is not None
    assert stored.last_heartbeat_at.replace(tzinfo=timezone.utc) == second
    assert stored.last_reconciled_at.replace(tzinfo=timezone.utc) == first
