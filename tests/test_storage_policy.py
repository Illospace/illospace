"""Tests for the runtime-editable storage policy."""

from __future__ import annotations

import importlib
from dataclasses import dataclass, fields, replace
from datetime import timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.schema import CreateIndex

from brain.platform.db.models.storage_policy import StoragePolicy
from brain.systems.storage_policy import (
    StoragePolicyPatch,
    StoragePolicyValues,
    async_get_storage_policy,
    async_manage_storage_policy,
    storage_policy_field_schema,
)


class _ScalarResult:
    def scalar_one(self):
        return 0


class _MigrationOperationsRecorder:
    def __init__(self):
        self.statements = []

    def get_bind(self):
        return self

    def execute(self, statement):
        if str(statement).lstrip().startswith("SELECT count(*)"):
            return _ScalarResult()
        self.statements.append(statement)
        return None


@pytest.mark.asyncio
async def test_fresh_storage_policy_seed_enables_automatic_reclamation(
    monkeypatch,
    async_sqlite_session_factory,
):
    migration = importlib.import_module(
        "brain.platform.db.alembic.versions.0060_storage_policies"
    )
    session = await async_sqlite_session_factory([StoragePolicy.__table__])
    operations = _MigrationOperationsRecorder()
    monkeypatch.setattr(migration, "_table_exists", lambda: True)
    monkeypatch.setattr(migration, "_index_exists", lambda _name: True)
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()
    assert len(operations.statements) == 1
    await session.execute(operations.statements[0])

    assert (
        await session.execute(
            sa.text(
                "SELECT automatic_reclamation_allowed, rationale "
                "FROM storage_policies WHERE is_active = TRUE"
            )
        )
    ).one() == (
        1,
        "Automatic workspace reclamation is enabled by default.",
    )


@pytest.mark.asyncio
async def test_enable_automatic_reclamation_migration_only_updates_untouched_seed(
    monkeypatch,
    async_sqlite_session_factory,
):
    migration = importlib.import_module(
        "brain.platform.db.alembic.versions.0065_enable_automatic_reclamation"
    )
    assert migration.revision == "0065_enable_automatic_reclamation"
    assert migration.down_revision == "0064_cycle_receipt_monitoring"
    session = await async_sqlite_session_factory([])
    seed_rationale = "Initial policy migrated from deployed retention behavior."

    await session.execute(
        sa.text(
            "CREATE TABLE storage_policies ("
            "id INTEGER PRIMARY KEY, "
            "automatic_reclamation_allowed BOOLEAN NOT NULL, "
            "rationale TEXT NOT NULL, "
            "is_active BOOLEAN NOT NULL)"
        )
    )
    await session.execute(
        sa.text(
            "INSERT INTO storage_policies VALUES "
            "(1, FALSE, :seed_rationale, TRUE), "
            "(2, FALSE, 'Operator deliberately disabled reclamation.', TRUE), "
            "(3, FALSE, :seed_rationale, FALSE)"
        ),
        {"seed_rationale": seed_rationale},
    )
    operations = _MigrationOperationsRecorder()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()
    migration.upgrade()
    for statement in operations.statements:
        await session.execute(statement)

    assert (
        await session.execute(
            sa.text(
                "SELECT id, automatic_reclamation_allowed, rationale "
                "FROM storage_policies ORDER BY id"
            )
        )
    ).all() == [
        (1, 1, "Illospace issue #876 enabled automatic reclamation."),
        (2, 0, "Operator deliberately disabled reclamation."),
        (3, 0, seed_rationale),
    ]

    operations.statements.clear()
    migration.downgrade()
    migration.downgrade()
    for statement in operations.statements:
        await session.execute(statement)

    assert (
        await session.execute(
            sa.text(
                "SELECT id, automatic_reclamation_allowed, rationale "
                "FROM storage_policies ORDER BY id"
            )
        )
    ).all() == [
        (1, 0, seed_rationale),
        (2, 0, "Operator deliberately disabled reclamation."),
        (3, 0, seed_rationale),
    ]


@pytest.fixture
async def session(async_sqlite_session_factory):
    session = await async_sqlite_session_factory([StoragePolicy.__table__])
    connection = await session.connection()
    for index in StoragePolicy.__table__.indexes:
        await connection.execute(CreateIndex(index, if_not_exists=True))
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


async def _active_row(session) -> StoragePolicy:
    row = await session.scalar(
        select(StoragePolicy).where(StoragePolicy.is_active.is_(True))
    )
    assert row is not None
    return row


@pytest.mark.asyncio
async def test_update_appends_audited_active_revision(session):
    original = await async_get_storage_policy(session)
    original_row = await _active_row(session)

    assert isinstance(original, StoragePolicyValues)
    assert original.finished_workspace_retention == timedelta(hours=48)
    assert original.project_draft_retention == timedelta(hours=168)
    assert original.canvas_quiet_period == timedelta(hours=24)

    result = await async_manage_storage_policy(
        session,
        action="update",
        patch=StoragePolicyPatch.from_storage_fields(
            {
                "finished_workspace_retention_hours": 72,
                "project_draft_retention_hours": 120,
                "capacity_warn_percent": 75,
                "capacity_critical_percent": 88,
                "automatic_reclamation_allowed": True,
            }
        ),
        rationale="Capacity is tight after the new workspace rollout",
        source_type="agent",
        source_id="run-779",
    )

    current = await async_get_storage_policy(session)
    current_row = await _active_row(session)
    assert current_row.id != original_row.id
    assert original_row.is_active is False
    assert current.finished_workspace_retention == timedelta(hours=72)
    assert current.project_draft_retention == timedelta(hours=120)
    assert current.canvas_quiet_period == timedelta(hours=24)
    assert current.capacity_warn_percent == 75
    assert current.capacity_critical_percent == 88
    assert current.automatic_reclamation_allowed is True
    assert current_row.rationale == "Capacity is tight after the new workspace rollout"
    assert current_row.source_type == "agent"
    assert current_row.source_id == "run-779"
    assert result["updated"]["id"] == current_row.id
    assert result["updated"]["finished_workspace_retention_hours"] == 72
    assert result["updated"]["project_draft_retention_hours"] == 120
    assert result["updated"]["canvas_quiet_hours"] == 24


@pytest.mark.asyncio
async def test_revert_appends_revision_copied_from_history(session):
    original = await async_get_storage_policy(session)
    original_row = await _active_row(session)
    await async_manage_storage_policy(
        session,
        action="update",
        patch=StoragePolicyPatch.from_storage_fields(
            {
                "finished_workspace_retention_hours": 24,
                "project_draft_retention_hours": 48,
                "canvas_quiet_hours": 12,
            }
        ),
        rationale="Shorten both retention windows",
        source_id="run-1",
    )

    result = await async_manage_storage_policy(
        session,
        action="revert",
        policy_id=original_row.id,
        rationale="Restore the previous safety window",
        source_id="run-2",
    )

    current = await async_get_storage_policy(session)
    current_row = await _active_row(session)
    history = list(
        (
            await session.scalars(
                select(StoragePolicy).order_by(StoragePolicy.id)
            )
        ).all()
    )
    assert len(history) == 3
    assert sum(row.is_active for row in history) == 1
    assert current_row.id == result["reverted"]["id"]
    assert current_row.id != original_row.id
    assert current_row.reverted_from_id == original_row.id
    assert current == original
    assert current_row.rationale == "Restore the previous safety window"


@pytest.mark.asyncio
async def test_update_requires_rationale_and_ordered_thresholds(session):
    with pytest.raises(ValueError, match="update requires at least one policy field"):
        await async_manage_storage_policy(
            session,
            action="update",
            rationale="No actual policy change",
        )

    with pytest.raises(ValueError, match="rationale is required"):
        await async_manage_storage_policy(
            session,
            action="update",
            patch=StoragePolicyPatch.from_storage_fields(
                {"finished_workspace_retention_hours": 24}
            ),
        )

    with pytest.raises(ValueError, match="must be less than"):
        await async_manage_storage_policy(
            session,
            action="update",
            patch=StoragePolicyPatch.from_storage_fields(
                {
                    "capacity_warn_percent": 95,
                    "capacity_critical_percent": 90,
                }
            ),
            rationale="Invalid threshold experiment",
        )


@pytest.mark.asyncio
async def test_dataclass_fields_drive_patch_storage_and_tool_schema(session):
    from brain.systems.runs.tool_definitions import COORDINATOR_TOOLS

    values = await async_get_storage_policy(session)
    value_field_names = tuple(policy_field.name for policy_field in fields(values))
    patch_field_names = tuple(
        patch_field.name for patch_field in fields(StoragePolicyPatch)
    )
    storage_field_names = set(values.to_storage_fields())
    generated_schema = storage_policy_field_schema()
    tool = next(
        tool for tool in COORDINATOR_TOOLS if tool["name"] == "manage_storage_policy"
    )
    tool_properties = tool["input_schema"]["properties"]

    assert patch_field_names == value_field_names
    assert set(generated_schema) == storage_field_names
    assert set(tool_properties) - {"action", "policy_id", "rationale", "limit"} == (
        storage_field_names
    )
    assert StoragePolicyPatch().is_empty()
    for policy_field in fields(values):
        patch = StoragePolicyPatch(
            **{policy_field.name: getattr(values, policy_field.name)}
        )
        assert not patch.is_empty()


def test_added_value_field_derives_patch_and_tool_schema():
    from brain.systems import storage_policy

    @dataclass(frozen=True)
    class ExtendedStoragePolicyValues(StoragePolicyValues):
        test_retention: timedelta = storage_policy._policy_value_field(
            "test_retention_hours",
            storage_policy._DURATION_HOURS,
            {"type": "integer", "minimum": 1},
        )

    patch_type = storage_policy._derive_storage_policy_patch(
        ExtendedStoragePolicyValues
    )

    assert "test_retention" in {
        patch_field.name for patch_field in fields(patch_type)
    }
    assert storage_policy.storage_policy_field_schema(
        ExtendedStoragePolicyValues
    )["test_retention_hours"] == {"type": "integer", "minimum": 1}
    assert patch_type.from_storage_fields(
        {"test_retention_hours": 6}
    ).test_retention == timedelta(hours=6)


@pytest.mark.asyncio
async def test_every_policy_field_survives_update_history_and_revert(session):
    original = await async_get_storage_policy(session)
    original_row = await _active_row(session)
    original_storage_fields = original.to_storage_fields()

    for policy_field in fields(original):
        current_value = getattr(original, policy_field.name)
        if isinstance(current_value, bool):
            changed_value = not current_value
        elif isinstance(current_value, timedelta):
            changed_value = current_value + timedelta(hours=1)
        elif isinstance(current_value, int):
            changed_value = current_value + 1
        else:
            raise AssertionError(
                f"Add a throwaway value for storage policy field {policy_field.name!r}"
            )

        expected = replace(
            original,
            **{policy_field.name: changed_value},
        ).validated()
        expected_storage_fields = expected.to_storage_fields()
        changed_storage_fields = {
            storage_name
            for storage_name, value in expected_storage_fields.items()
            if original_storage_fields[storage_name] != value
        }
        assert len(changed_storage_fields) == 1
        storage_name = changed_storage_fields.pop()

        updated = await async_manage_storage_policy(
            session,
            action="update",
            rationale=f"Exercise derived field coverage for {storage_name}",
            patch=StoragePolicyPatch.from_storage_fields(
                {storage_name: expected_storage_fields[storage_name]}
            ),
        )
        assert await async_get_storage_policy(session) == expected
        assert updated["updated"][storage_name] == expected_storage_fields[storage_name]

        history = await async_manage_storage_policy(session, action="history")
        assert history["policies"][0][storage_name] == expected_storage_fields[storage_name]

        reverted = await async_manage_storage_policy(
            session,
            action="revert",
            policy_id=original_row.id,
            rationale=f"Restore derived field coverage for {storage_name}",
        )
        assert await async_get_storage_policy(session) == original
        assert reverted["reverted"][storage_name] == original_storage_fields[storage_name]
