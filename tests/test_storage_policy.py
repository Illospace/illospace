"""Tests for the runtime-editable storage policy."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from brain.platform.db.models.storage_policy import StoragePolicy
from brain.systems.storage_policy import (
    async_get_storage_policy,
    async_manage_storage_policy,
)


@pytest.fixture
async def session(async_sqlite_session_factory):
    session = await async_sqlite_session_factory([StoragePolicy.__table__])
    connection = await session.connection()
    for index in StoragePolicy.__table__.indexes:
        await connection.run_sync(
            lambda sync_connection, policy_index=index: policy_index.create(
                sync_connection,
                checkfirst=True,
            )
        )
    session.add(
        StoragePolicy(
            finished_workspace_retention_hours=48,
            project_draft_retention_hours=168,
            canvas_quiet_hours=24,
            capacity_warn_percent=80,
            capacity_critical_percent=90,
            automatic_reclamation_allowed=False,
            rationale="Initial migrated behavior",
            source_type="system",
            is_active=True,
        )
    )
    await session.flush()
    return session


@pytest.mark.asyncio
async def test_update_appends_audited_active_revision(session):
    original = await async_get_storage_policy(session)

    result = await async_manage_storage_policy(
        session,
        action="update",
        finished_workspace_retention_hours=72,
        project_draft_retention_hours=120,
        capacity_warn_percent=75,
        capacity_critical_percent=88,
        automatic_reclamation_allowed=True,
        rationale="Capacity is tight after the new workspace rollout",
        source_type="agent",
        source_id="run-779",
    )

    current = await async_get_storage_policy(session)
    assert current.id != original.id
    assert original.is_active is False
    assert current.finished_workspace_retention_hours == 72
    assert current.project_draft_retention_hours == 120
    assert current.canvas_quiet_hours == 24
    assert current.capacity_warn_percent == 75
    assert current.capacity_critical_percent == 88
    assert current.automatic_reclamation_allowed is True
    assert current.rationale == "Capacity is tight after the new workspace rollout"
    assert current.source_type == "agent"
    assert current.source_id == "run-779"
    assert result["updated"]["id"] == current.id


@pytest.mark.asyncio
async def test_revert_appends_revision_copied_from_history(session):
    original = await async_get_storage_policy(session)
    await async_manage_storage_policy(
        session,
        action="update",
        finished_workspace_retention_hours=24,
        project_draft_retention_hours=48,
        canvas_quiet_hours=12,
        rationale="Shorten both retention windows",
        source_id="run-1",
    )

    result = await async_manage_storage_policy(
        session,
        action="revert",
        policy_id=original.id,
        rationale="Restore the previous safety window",
        source_id="run-2",
    )

    current = await async_get_storage_policy(session)
    history = list(
        (
            await session.scalars(
                select(StoragePolicy).order_by(StoragePolicy.id)
            )
        ).all()
    )
    assert len(history) == 3
    assert sum(row.is_active for row in history) == 1
    assert current.id == result["reverted"]["id"]
    assert current.id != original.id
    assert current.reverted_from_id == original.id
    assert (
        current.finished_workspace_retention_hours
        == original.finished_workspace_retention_hours
    )
    assert current.project_draft_retention_hours == original.project_draft_retention_hours
    assert current.canvas_quiet_hours == original.canvas_quiet_hours
    assert current.rationale == "Restore the previous safety window"


@pytest.mark.asyncio
async def test_update_requires_rationale_and_ordered_thresholds(session):
    with pytest.raises(ValueError, match="rationale is required"):
        await async_manage_storage_policy(
            session,
            action="update",
            finished_workspace_retention_hours=24,
        )

    with pytest.raises(ValueError, match="must be less than"):
        await async_manage_storage_policy(
            session,
            action="update",
            capacity_warn_percent=95,
            capacity_critical_percent=90,
            rationale="Invalid threshold experiment",
        )
