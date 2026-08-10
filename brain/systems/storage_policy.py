"""Read and revise the installation-wide storage policy."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.storage_policy import StoragePolicy

__all__ = [
    "async_get_storage_policy",
    "async_list_storage_policy_history",
    "async_manage_storage_policy",
    "async_revert_storage_policy",
    "async_update_storage_policy",
    "serialize_storage_policy",
]


def _positive_hours(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be a positive integer")
    normalized = value
    if normalized <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return normalized


def _percent(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field} must be an integer from 1 to 100")
    normalized = value
    if normalized < 1 or normalized > 100:
        raise ValueError(f"{field} must be an integer from 1 to 100")
    return normalized


def _required_text(value: object, field: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field} is required")
    return normalized


def serialize_storage_policy(row: StoragePolicy) -> dict[str, Any]:
    """Return the stable runtime policy payload exposed to Illo."""

    created_at = getattr(row, "created_at", None)
    return {
        "id": row.id,
        "finished_workspace_retention_hours": row.finished_workspace_retention_hours,
        "project_draft_retention_hours": row.project_draft_retention_hours,
        "canvas_quiet_hours": row.canvas_quiet_hours,
        "capacity_warn_percent": row.capacity_warn_percent,
        "capacity_critical_percent": row.capacity_critical_percent,
        "automatic_reclamation_allowed": row.automatic_reclamation_allowed,
        "rationale": row.rationale,
        "source_type": row.source_type,
        "source_id": row.source_id,
        "reverted_from_id": row.reverted_from_id,
        "is_active": row.is_active,
        "created_at": created_at.isoformat() if created_at is not None else None,
    }


async def async_get_storage_policy(
    session: AsyncSession,
    *,
    for_update: bool = False,
) -> StoragePolicy:
    """Return the one active policy or fail closed when migration state is invalid."""

    statement = (
        select(StoragePolicy)
        .where(StoragePolicy.is_active.is_(True))
        .order_by(StoragePolicy.id.desc())
        .limit(1)
    )
    if for_update:
        statement = statement.with_for_update()
    row = await session.scalar(statement)
    if row is None:
        raise RuntimeError("No active storage policy is configured")
    return row


async def async_list_storage_policy_history(
    session: AsyncSession,
    *,
    limit: int = 50,
) -> list[StoragePolicy]:
    """Return newest-first immutable policy revisions."""

    normalized_limit = max(1, min(int(limit), 200))
    rows = await session.scalars(
        select(StoragePolicy)
        .order_by(StoragePolicy.created_at.desc(), StoragePolicy.id.desc())
        .limit(normalized_limit)
    )
    return list(rows.all())


async def _append_storage_policy(
    session: AsyncSession,
    *,
    current: StoragePolicy,
    finished_workspace_retention_hours: object,
    project_draft_retention_hours: object,
    canvas_quiet_hours: object,
    capacity_warn_percent: object,
    capacity_critical_percent: object,
    automatic_reclamation_allowed: object,
    rationale: object,
    source_type: object,
    source_id: object,
    reverted_from_id: int | None = None,
) -> StoragePolicy:
    finished_workspace_hours = _positive_hours(
        finished_workspace_retention_hours,
        "finished_workspace_retention_hours",
    )
    project_draft_hours = _positive_hours(
        project_draft_retention_hours,
        "project_draft_retention_hours",
    )
    canvas_hours = _positive_hours(canvas_quiet_hours, "canvas_quiet_hours")
    warn_percent = _percent(capacity_warn_percent, "capacity_warn_percent")
    critical_percent = _percent(
        capacity_critical_percent,
        "capacity_critical_percent",
    )
    if warn_percent >= critical_percent:
        raise ValueError(
            "capacity_warn_percent must be less than capacity_critical_percent"
        )
    if not isinstance(automatic_reclamation_allowed, bool):
        raise ValueError("automatic_reclamation_allowed must be a boolean")

    row = StoragePolicy(
        finished_workspace_retention_hours=finished_workspace_hours,
        project_draft_retention_hours=project_draft_hours,
        canvas_quiet_hours=canvas_hours,
        capacity_warn_percent=warn_percent,
        capacity_critical_percent=critical_percent,
        automatic_reclamation_allowed=automatic_reclamation_allowed,
        rationale=_required_text(rationale, "rationale"),
        source_type=_required_text(source_type, "source_type"),
        source_id=str(source_id).strip() if source_id is not None else None,
        reverted_from_id=reverted_from_id,
        is_active=True,
    )
    current.is_active = False
    session.add(row)
    await session.flush()
    return row


async def async_update_storage_policy(
    session: AsyncSession,
    *,
    rationale: str,
    source_type: str,
    source_id: str | None,
    finished_workspace_retention_hours: int | None = None,
    project_draft_retention_hours: int | None = None,
    canvas_quiet_hours: int | None = None,
    capacity_warn_percent: int | None = None,
    capacity_critical_percent: int | None = None,
    automatic_reclamation_allowed: bool | None = None,
) -> StoragePolicy:
    """Append one audited revision and make it the active policy."""

    current = await async_get_storage_policy(session, for_update=True)
    return await _append_storage_policy(
        session,
        current=current,
        finished_workspace_retention_hours=(
            current.finished_workspace_retention_hours
            if finished_workspace_retention_hours is None
            else finished_workspace_retention_hours
        ),
        project_draft_retention_hours=(
            current.project_draft_retention_hours
            if project_draft_retention_hours is None
            else project_draft_retention_hours
        ),
        canvas_quiet_hours=(
            current.canvas_quiet_hours
            if canvas_quiet_hours is None
            else canvas_quiet_hours
        ),
        capacity_warn_percent=(
            current.capacity_warn_percent
            if capacity_warn_percent is None
            else capacity_warn_percent
        ),
        capacity_critical_percent=(
            current.capacity_critical_percent
            if capacity_critical_percent is None
            else capacity_critical_percent
        ),
        automatic_reclamation_allowed=(
            current.automatic_reclamation_allowed
            if automatic_reclamation_allowed is None
            else automatic_reclamation_allowed
        ),
        rationale=rationale,
        source_type=source_type,
        source_id=source_id,
    )


async def async_revert_storage_policy(
    session: AsyncSession,
    *,
    policy_id: int,
    rationale: str,
    source_type: str,
    source_id: str | None,
) -> StoragePolicy:
    """Append a new active revision with values copied from an older revision."""

    current = await async_get_storage_policy(session, for_update=True)
    target = await session.get(StoragePolicy, int(policy_id))
    if target is None:
        raise ValueError(f"Storage policy {policy_id} not found")
    return await _append_storage_policy(
        session,
        current=current,
        finished_workspace_retention_hours=target.finished_workspace_retention_hours,
        project_draft_retention_hours=target.project_draft_retention_hours,
        canvas_quiet_hours=target.canvas_quiet_hours,
        capacity_warn_percent=target.capacity_warn_percent,
        capacity_critical_percent=target.capacity_critical_percent,
        automatic_reclamation_allowed=target.automatic_reclamation_allowed,
        rationale=rationale,
        source_type=source_type,
        source_id=source_id,
        reverted_from_id=target.id,
    )


async def async_manage_storage_policy(
    session: AsyncSession,
    *,
    action: str = "get",
    rationale: str | None = None,
    source_type: str = "agent",
    source_id: str | None = None,
    policy_id: int | None = None,
    finished_workspace_retention_hours: int | None = None,
    project_draft_retention_hours: int | None = None,
    canvas_quiet_hours: int | None = None,
    capacity_warn_percent: int | None = None,
    capacity_critical_percent: int | None = None,
    automatic_reclamation_allowed: bool | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Agent-facing read, update, history, and revert actions."""

    normalized_action = str(action or "get").strip().lower()
    if normalized_action == "get":
        return {"policy": serialize_storage_policy(await async_get_storage_policy(session))}
    if normalized_action == "history":
        rows = await async_list_storage_policy_history(session, limit=limit)
        return {"policies": [serialize_storage_policy(row) for row in rows]}
    if normalized_action == "update":
        if all(
            value is None
            for value in (
                finished_workspace_retention_hours,
                project_draft_retention_hours,
                canvas_quiet_hours,
                capacity_warn_percent,
                capacity_critical_percent,
                automatic_reclamation_allowed,
            )
        ):
            raise ValueError("update requires at least one policy field")
        row = await async_update_storage_policy(
            session,
            rationale=_required_text(rationale, "rationale"),
            source_type=source_type,
            source_id=source_id,
            finished_workspace_retention_hours=finished_workspace_retention_hours,
            project_draft_retention_hours=project_draft_retention_hours,
            canvas_quiet_hours=canvas_quiet_hours,
            capacity_warn_percent=capacity_warn_percent,
            capacity_critical_percent=capacity_critical_percent,
            automatic_reclamation_allowed=automatic_reclamation_allowed,
        )
        return {"updated": serialize_storage_policy(row)}
    if normalized_action == "revert":
        if policy_id is None:
            raise ValueError("policy_id is required")
        row = await async_revert_storage_policy(
            session,
            policy_id=policy_id,
            rationale=_required_text(rationale, "rationale"),
            source_type=source_type,
            source_id=source_id,
        )
        return {"reverted": serialize_storage_policy(row)}
    raise ValueError("manage_storage_policy action must be get, update, history, or revert")
