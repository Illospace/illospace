"""Read and revise the installation-wide storage policy."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields, replace
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.storage_policy import StoragePolicy

__all__ = [
    "StoragePolicyPatch",
    "StoragePolicyValues",
    "async_get_storage_policy",
    "async_list_storage_policy_history",
    "async_manage_storage_policy",
    "async_revert_storage_policy",
    "async_update_storage_policy",
    "serialize_storage_policy",
    "storage_policy_field_schema",
]

_STORAGE_NAME = "storage_name"
_STORAGE_KIND = "storage_kind"
_INPUT_SCHEMA = "input_schema"
_DURATION_HOURS = "duration_hours"
_PERCENT = "percent"
_BOOLEAN = "boolean"


def _policy_value_field(
    storage_name: str,
    storage_kind: str,
    input_schema: Mapping[str, Any],
):
    return field(
        metadata={
            _STORAGE_NAME: storage_name,
            _STORAGE_KIND: storage_kind,
            _INPUT_SCHEMA: dict(input_schema),
        }
    )


@dataclass(frozen=True)
class StoragePolicyValues:
    """Canonical policy values, converted from storage units for consumers."""

    finished_workspace_retention: timedelta = _policy_value_field(
        "finished_workspace_retention_hours",
        _DURATION_HOURS,
        {
            "type": "integer",
            "minimum": 1,
            "description": "Retention window for finished agent workspaces.",
        },
    )
    project_draft_retention: timedelta = _policy_value_field(
        "project_draft_retention_hours",
        _DURATION_HOURS,
        {
            "type": "integer",
            "minimum": 1,
            "description": "Retention window for unpublished Project drafts.",
        },
    )
    canvas_quiet_period: timedelta = _policy_value_field(
        "canvas_quiet_hours",
        _DURATION_HOURS,
        {
            "type": "integer",
            "minimum": 1,
            "description": "Quiet window before emerged canvas thoughts are archived.",
        },
    )
    capacity_warn_percent: int = _policy_value_field(
        "capacity_warn_percent",
        _PERCENT,
        {
            "type": "integer",
            "minimum": 1,
            "maximum": 99,
            "description": "Storage-use percentage that starts a warning state.",
        },
    )
    capacity_critical_percent: int = _policy_value_field(
        "capacity_critical_percent",
        _PERCENT,
        {
            "type": "integer",
            "minimum": 2,
            "maximum": 100,
            "description": "Storage-use percentage that starts a critical state.",
        },
    )
    automatic_reclamation_allowed: bool = _policy_value_field(
        "automatic_reclamation_allowed",
        _BOOLEAN,
        {"type": "boolean"},
    )

    @classmethod
    def from_row(cls, row: StoragePolicy) -> StoragePolicyValues:
        """Build typed values from one database revision."""

        values = {
            policy_field.name: _decode_storage_value(
                policy_field,
                getattr(row, _storage_name(policy_field)),
            )
            for policy_field in fields(cls)
        }
        return cls(**values).validated()

    def validated(self) -> StoragePolicyValues:
        """Return these values after enforcing the storage-policy invariants."""

        for policy_field in fields(self):
            _encode_storage_value(
                policy_field,
                getattr(self, policy_field.name),
            )
        if self.capacity_warn_percent >= self.capacity_critical_percent:
            raise ValueError(
                "capacity_warn_percent must be less than capacity_critical_percent"
            )
        return self

    def patched(self, patch: StoragePolicyPatch) -> StoragePolicyValues:
        """Overlay the set patch fields and validate the complete policy."""

        updates = {
            policy_field.name: value
            for policy_field in fields(self)
            if (value := getattr(patch, policy_field.name)) is not None
        }
        return replace(self, **updates).validated()

    def to_storage_fields(self) -> dict[str, int | bool]:
        """Encode all policy values for the ORM and external response boundary."""

        self.validated()
        return {
            _storage_name(policy_field): _encode_storage_value(
                policy_field,
                getattr(self, policy_field.name),
            )
            for policy_field in fields(self)
        }


@dataclass(frozen=True)
class StoragePolicyPatch:
    """Typed optional changes to storage policy values."""

    finished_workspace_retention: timedelta | None = None
    project_draft_retention: timedelta | None = None
    canvas_quiet_period: timedelta | None = None
    capacity_warn_percent: int | None = None
    capacity_critical_percent: int | None = None
    automatic_reclamation_allowed: bool | None = None

    @classmethod
    def from_storage_fields(
        cls,
        storage_values: Mapping[str, object],
    ) -> StoragePolicyPatch:
        """Decode tool and route field names into the typed write shape."""

        fields_by_storage_name = {
            _storage_name(policy_field): policy_field
            for policy_field in fields(StoragePolicyValues)
        }
        unexpected = set(storage_values) - fields_by_storage_name.keys()
        if unexpected:
            unexpected_names = ", ".join(sorted(unexpected))
            raise TypeError(f"Unexpected storage policy fields: {unexpected_names}")
        patch_values = {
            policy_field.name: _decode_storage_value(
                policy_field,
                storage_values[storage_name],
            )
            for storage_name, policy_field in fields_by_storage_name.items()
            if storage_name in storage_values
            and storage_values[storage_name] is not None
        }
        return cls(**patch_values)

    def is_empty(self) -> bool:
        return all(
            getattr(self, patch_field.name) is None
            for patch_field in fields(self)
        )


def _positive_hours(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be a positive integer")
    if value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")
    return value


def _duration_hours(value: object, field_name: str) -> int:
    if not isinstance(value, timedelta):
        raise ValueError(f"{field_name} must be a positive whole-hour duration")
    seconds = value.total_seconds()
    if seconds <= 0 or seconds % 3600:
        raise ValueError(f"{field_name} must be a positive whole-hour duration")
    return int(seconds // 3600)


def _percent(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be an integer from 1 to 100")
    if value < 1 or value > 100:
        raise ValueError(f"{field_name} must be an integer from 1 to 100")
    return value


def _required_text(value: object, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def _storage_name(policy_field) -> str:
    return str(policy_field.metadata[_STORAGE_NAME])


def _decode_storage_value(policy_field, value: object) -> Any:
    storage_name = _storage_name(policy_field)
    storage_kind = policy_field.metadata[_STORAGE_KIND]
    if storage_kind == _DURATION_HOURS:
        return timedelta(hours=_positive_hours(value, storage_name))
    return value


def _encode_storage_value(policy_field, value: object) -> int | bool:
    storage_name = _storage_name(policy_field)
    storage_kind = policy_field.metadata[_STORAGE_KIND]
    if storage_kind == _DURATION_HOURS:
        return _duration_hours(value, storage_name)
    if storage_kind == _PERCENT:
        return _percent(value, storage_name)
    if storage_kind == _BOOLEAN:
        if not isinstance(value, bool):
            raise ValueError(f"{storage_name} must be a boolean")
        return value
    raise RuntimeError(f"Unsupported storage policy field kind: {storage_kind}")


def storage_policy_field_schema() -> dict[str, dict[str, Any]]:
    """Return tool input schemas for every writable policy value."""

    return {
        _storage_name(policy_field): dict(policy_field.metadata[_INPUT_SCHEMA])
        for policy_field in fields(StoragePolicyValues)
    }


def serialize_storage_policy(row: StoragePolicy) -> dict[str, Any]:
    """Return the stable runtime policy payload exposed to Illo."""

    created_at = getattr(row, "created_at", None)
    return {
        "id": row.id,
        **StoragePolicyValues.from_row(row).to_storage_fields(),
        "rationale": row.rationale,
        "source_type": row.source_type,
        "source_id": row.source_id,
        "reverted_from_id": row.reverted_from_id,
        "is_active": row.is_active,
        "created_at": created_at.isoformat() if created_at is not None else None,
    }


async def _async_get_storage_policy_row(
    session: AsyncSession,
    *,
    for_update: bool = False,
) -> StoragePolicy:
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


async def async_get_storage_policy(
    session: AsyncSession,
    *,
    for_update: bool = False,
) -> StoragePolicyValues:
    """Return typed values for the one active policy."""

    row = await _async_get_storage_policy_row(session, for_update=for_update)
    return StoragePolicyValues.from_row(row)


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
    values: StoragePolicyValues,
    rationale: object,
    source_type: object,
    source_id: object,
    reverted_from_id: int | None = None,
) -> StoragePolicy:
    row = StoragePolicy(
        **values.to_storage_fields(),
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
    patch: StoragePolicyPatch,
    rationale: str,
    source_type: str,
    source_id: str | None,
) -> StoragePolicy:
    """Append one audited revision and make it the active policy."""

    current = await _async_get_storage_policy_row(session, for_update=True)
    current_values = StoragePolicyValues.from_row(current)
    return await _append_storage_policy(
        session,
        current=current,
        values=current_values.patched(patch),
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

    current = await _async_get_storage_policy_row(session, for_update=True)
    target = await session.get(StoragePolicy, int(policy_id))
    if target is None:
        raise ValueError(f"Storage policy {policy_id} not found")
    return await _append_storage_policy(
        session,
        current=current,
        values=StoragePolicyValues.from_row(target),
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
    limit: int = 50,
    patch: StoragePolicyPatch | None = None,
    **storage_values: object,
) -> dict[str, Any]:
    """Agent-facing read, update, history, and revert actions."""

    normalized_action = str(action or "get").strip().lower()
    if normalized_action == "get":
        row = await _async_get_storage_policy_row(session)
        return {"policy": serialize_storage_policy(row)}
    if normalized_action == "history":
        rows = await async_list_storage_policy_history(session, limit=limit)
        return {"policies": [serialize_storage_policy(row) for row in rows]}
    if normalized_action == "update":
        if patch is not None and storage_values:
            raise TypeError("Pass a storage policy patch or storage fields, not both")
        update_patch = patch or StoragePolicyPatch.from_storage_fields(storage_values)
        if update_patch.is_empty():
            raise ValueError("update requires at least one policy field")
        row = await async_update_storage_policy(
            session,
            patch=update_patch,
            rationale=_required_text(rationale, "rationale"),
            source_type=source_type,
            source_id=source_id,
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
