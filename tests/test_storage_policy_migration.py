"""Migration coverage for the storage-policy seed and its reclamation flip."""

from __future__ import annotations

import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import sqlalchemy as sa

from brain.platform.db.models.storage_policy import StoragePolicy

_SEED_RATIONALE = "Initial policy migrated from deployed retention behavior."
_ENABLED_RATIONALE = "Illospace issue #876 enabled automatic reclamation."
_OPERATOR_RATIONALE = "Operator deliberately disabled reclamation."
_POLICY_ROWS_SQL = sa.text(
    "SELECT id, automatic_reclamation_allowed, rationale, source_type, is_active "
    "FROM storage_policies ORDER BY id"
)


def _policy_row(**overrides):
    row = {
        "finished_workspace_retention_hours": 48,
        "project_draft_retention_hours": 168,
        "canvas_quiet_hours": 24,
        "capacity_warn_percent": 80,
        "capacity_critical_percent": 90,
        "automatic_reclamation_allowed": False,
        "rationale": _SEED_RATIONALE,
        "source_type": "operator",
        "is_active": True,
    }
    row.update(overrides)
    return row


def _migration(name: str):
    return importlib.import_module(f"brain.platform.db.alembic.versions.{name}")


@pytest.mark.parametrize("baseline_creates_table", [False, True])
def test_storage_policy_migration_seeds_only_when_no_active_policy_exists(
    monkeypatch,
    baseline_creates_table,
):
    migration = _migration("0060_storage_policies")
    assert migration.revision == "0060_storage_policies"
    assert migration.down_revision == "0059_behavior_change_audits"
    engine = sa.create_engine("sqlite://")

    with engine.begin() as connection:
        if baseline_creates_table:
            # The public baseline creates model tables before migrations replay.
            StoragePolicy.__table__.create(connection)
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(migration, "op", operations)

        migration.upgrade()
        seeded = [(1, 0, _SEED_RATIONALE, "system", 1)]
        assert connection.execute(_POLICY_ROWS_SQL).all() == seeded

        # An active policy already exists, so the guard must not seed again.
        migration.upgrade()
        assert connection.execute(_POLICY_ROWS_SQL).all() == seeded

        # An operator revision that replaced the seed also blocks the seed.
        connection.execute(
            sa.update(StoragePolicy.__table__)
            .where(StoragePolicy.__table__.c.id == 1)
            .values(is_active=False)
        )
        connection.execute(
            sa.insert(StoragePolicy.__table__).values(
                _policy_row(rationale=_OPERATOR_RATIONALE)
            )
        )
        migration.upgrade()
        assert connection.execute(_POLICY_ROWS_SQL).all() == [
            (1, 0, _SEED_RATIONALE, "system", 0),
            (2, 0, _OPERATOR_RATIONALE, "operator", 1),
        ]

        migration.downgrade()
        migration.downgrade()
        assert "storage_policies" not in sa.inspect(connection).get_table_names()


def test_enable_automatic_reclamation_migration_flips_untouched_seed_and_round_trips(
    monkeypatch,
):
    seed_migration = _migration("0060_storage_policies")
    migration = _migration("0065_enable_automatic_reclamation")
    assert migration.revision == "0065_enable_automatic_reclamation"
    assert migration.down_revision == "0064_cycle_receipt_monitoring"
    engine = sa.create_engine("sqlite://")

    with engine.begin() as connection:
        StoragePolicy.__table__.create(connection)
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(seed_migration, "op", operations)
        monkeypatch.setattr(migration, "op", operations)

        # Fresh install: 0060 seeds the disabled policy, then 0065 flips it.
        seed_migration.upgrade()
        connection.execute(
            sa.insert(StoragePolicy.__table__).values(
                _policy_row(is_active=False)
            )
        )

        migration.upgrade()
        migration.upgrade()
        assert connection.execute(_POLICY_ROWS_SQL).all() == [
            (1, 1, _ENABLED_RATIONALE, "system", 1),
            (2, 0, _SEED_RATIONALE, "operator", 0),
        ]

        migration.downgrade()
        migration.downgrade()
        assert connection.execute(_POLICY_ROWS_SQL).all() == [
            (1, 0, _SEED_RATIONALE, "system", 1),
            (2, 0, _SEED_RATIONALE, "operator", 0),
        ]


def test_enable_automatic_reclamation_migration_leaves_operator_policy_alone(
    monkeypatch,
):
    migration = _migration("0065_enable_automatic_reclamation")
    engine = sa.create_engine("sqlite://")

    with engine.begin() as connection:
        StoragePolicy.__table__.create(connection)
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(migration, "op", operations)
        connection.execute(
            sa.insert(StoragePolicy.__table__).values(
                _policy_row(source_type="system", is_active=False)
            )
        )
        connection.execute(
            sa.insert(StoragePolicy.__table__).values(
                _policy_row(rationale=_OPERATOR_RATIONALE)
            )
        )
        untouched = [
            (1, 0, _SEED_RATIONALE, "system", 0),
            (2, 0, _OPERATOR_RATIONALE, "operator", 1),
        ]

        migration.upgrade()
        assert connection.execute(_POLICY_ROWS_SQL).all() == untouched

        migration.downgrade()
        assert connection.execute(_POLICY_ROWS_SQL).all() == untouched
