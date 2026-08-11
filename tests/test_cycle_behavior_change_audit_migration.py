"""Migration coverage for the Cycle-only behavior-change audit envelope."""

from __future__ import annotations

import importlib
from unittest.mock import Mock

import pytest


MIGRATION_MODULE = (
    "brain.platform.db.alembic.versions.0062_cycle_behavior_change_audits"
)


def _migration_with_tables(monkeypatch, *, old_exists: bool, new_exists: bool):
    migration = importlib.import_module(MIGRATION_MODULE)
    operations = Mock()
    monkeypatch.setattr(migration, "op", operations)
    monkeypatch.setattr(
        migration,
        "_has_table",
        lambda table_name: {
            migration._OLD_TABLE: old_exists,
            migration._TABLE: new_exists,
        }[table_name],
    )
    return migration, operations


def _executed_sql(operations: Mock) -> list[str]:
    return [str(call.args[0]) for call in operations.execute.call_args_list]


def test_live_upgrade_renames_in_place_and_drops_generic_columns(monkeypatch):
    migration, operations = _migration_with_tables(
        monkeypatch,
        old_exists=True,
        new_exists=False,
    )

    migration.upgrade()

    sql = _executed_sql(operations)
    assert sql[0] == (
        'ALTER TABLE "behavior_change_audits" '
        'RENAME TO "cycle_behavior_change_audits"'
    )
    assert (
        'ALTER TABLE "cycle_behavior_change_audits" '
        'DROP COLUMN "policy_kind"'
    ) in sql
    assert (
        'ALTER TABLE "cycle_behavior_change_audits" '
        'DROP COLUMN "target_type"'
    ) in sql
    assert len([statement for statement in sql if "RENAME CONSTRAINT" in statement]) == 2
    assert len([statement for statement in sql if "ALTER INDEX" in statement]) == 2
    operations.create_table.assert_not_called()
    operations.drop_table.assert_not_called()


def test_fresh_upgrade_removes_only_empty_historical_table(monkeypatch):
    migration, operations = _migration_with_tables(
        monkeypatch,
        old_exists=True,
        new_exists=True,
    )
    operations.get_bind.return_value.execute.return_value.scalar_one.return_value = 0

    migration.upgrade()

    operations.drop_table.assert_called_once_with("behavior_change_audits")
    operations.execute.assert_not_called()


def test_fresh_upgrade_refuses_to_discard_historical_rows(monkeypatch):
    migration, operations = _migration_with_tables(
        monkeypatch,
        old_exists=True,
        new_exists=True,
    )
    operations.get_bind.return_value.execute.return_value.scalar_one.return_value = 1

    with pytest.raises(RuntimeError, match="refusing to discard"):
        migration.upgrade()

    operations.drop_table.assert_not_called()


def test_downgrade_restores_historical_shape_and_names(monkeypatch):
    migration, operations = _migration_with_tables(
        monkeypatch,
        old_exists=False,
        new_exists=True,
    )

    migration.downgrade()

    restored_columns = {
        call.args[1].name: call.args[1]
        for call in operations.add_column.call_args_list
    }
    assert set(restored_columns) == {"policy_kind", "target_type"}
    assert all(column.nullable is False for column in restored_columns.values())
    assert all(column.server_default is not None for column in restored_columns.values())
    assert _executed_sql(operations)[-1] == (
        'ALTER TABLE "cycle_behavior_change_audits" '
        'RENAME TO "behavior_change_audits"'
    )
    operations.create_unique_constraint.assert_called_once_with(
        "uq_behavior_change_audits_target_version",
        "cycle_behavior_change_audits",
        ["policy_kind", "target_type", "target_id", "version"],
    )
    operations.create_index.assert_called_once_with(
        "ix_behavior_change_audits_target_history",
        "cycle_behavior_change_audits",
        ["policy_kind", "target_type", "target_id", "version"],
    )
