"""Migration coverage for Cycle schedule executor and skill bindings."""
from __future__ import annotations

import importlib
from unittest.mock import Mock


MIGRATION_MODULE = (
    "brain.platform.db.alembic.versions.0063_cycle_schedule_bindings"
)


def _migration(monkeypatch, *, columns=(), constraints=()):
    migration = importlib.import_module(MIGRATION_MODULE)
    operations = Mock()
    monkeypatch.setattr(migration, "op", operations)
    monkeypatch.setattr(migration, "_column_names", lambda: set(columns))
    monkeypatch.setattr(
        migration,
        "_check_constraint_names",
        lambda: set(constraints),
    )
    monkeypatch.setattr(migration, "_json_type", lambda: migration.sa.JSON())
    monkeypatch.setattr(
        migration,
        "_empty_list_default",
        lambda: migration.sa.text("'[]'::jsonb"),
    )
    return migration, operations


def test_revision_follows_cycle_behavior_audits():
    migration = importlib.import_module(MIGRATION_MODULE)

    assert migration.revision == "0063_cycle_schedule_bindings"
    assert migration.down_revision == "0062_cycle_behavior_change_audits"


def test_upgrade_adds_backfilled_bindings_and_database_constraint(monkeypatch):
    migration, operations = _migration(monkeypatch)
    create_constraint = Mock()
    monkeypatch.setattr(migration, "_create_executor_constraint", create_constraint)

    migration.upgrade()

    columns = {
        call.args[1].name: call.args[1]
        for call in operations.add_column.call_args_list
    }
    assert set(columns) == {"executor_binding", "skill_ids"}
    assert columns["executor_binding"].nullable is False
    assert str(columns["executor_binding"].server_default.arg) == "'illo-lane'"
    assert columns["skill_ids"].nullable is False
    assert str(columns["skill_ids"].server_default.arg) == "'[]'::jsonb"
    create_constraint.assert_called_once_with()


def test_upgrade_is_safe_after_fresh_baseline_materializes_current_model(monkeypatch):
    migration, operations = _migration(
        monkeypatch,
        columns={"executor_binding", "skill_ids"},
        constraints={"ck_cycles_executor_binding"},
    )
    create_constraint = Mock()
    monkeypatch.setattr(migration, "_create_executor_constraint", create_constraint)

    migration.upgrade()

    operations.add_column.assert_not_called()
    create_constraint.assert_not_called()


def test_downgrade_removes_constraint_before_both_columns(monkeypatch):
    migration, operations = _migration(
        monkeypatch,
        columns={"executor_binding", "skill_ids"},
        constraints={"ck_cycles_executor_binding"},
    )
    drop_constraint = Mock()
    monkeypatch.setattr(migration, "_drop_executor_constraint", drop_constraint)

    migration.downgrade()

    drop_constraint.assert_called_once_with()
    assert [call.args for call in operations.drop_column.call_args_list] == [
        ("cycles", "skill_ids"),
        ("cycles", "executor_binding"),
    ]
