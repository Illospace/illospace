"""Focused migration coverage for Cycle liveness controls."""

from __future__ import annotations

import importlib
from types import SimpleNamespace

from brain.platform.db.models.cycle import Cycle


def test_cycle_liveness_controls_migration_is_single_head_successor():
    migration = importlib.import_module(
        "brain.platform.db.alembic.versions.0043_cycle_liveness_controls"
    )

    assert migration.revision == "0043_cycle_liveness_controls"
    assert migration.down_revision == "0042_scheduler_failure_rate_guard"


def test_cycle_liveness_controls_migration_declares_safe_defaults(monkeypatch):
    migration = importlib.import_module(
        "brain.platform.db.alembic.versions.0043_cycle_liveness_controls"
    )

    class FakeOperations:
        def __init__(self):
            self.columns = {}
            self.bind = SimpleNamespace(
                dialect=SimpleNamespace(name="postgresql")
            )

        def get_bind(self):
            return self.bind

        def add_column(self, table_name, column):
            assert table_name == "cycles"
            self.columns[column.name] = column

    operations = FakeOperations()
    monkeypatch.setattr(migration, "op", operations)
    monkeypatch.setattr(
        migration,
        "_column_exists",
        lambda column_name: column_name in operations.columns,
    )

    migration.upgrade()
    migration.upgrade()

    assert operations.columns["timeout_seconds"].nullable is True
    assert operations.columns["timeout_seconds"].server_default is None
    assert operations.columns["retry_policy"].nullable is False
    assert str(operations.columns["retry_policy"].server_default.arg) == "'{}'::jsonb"
    assert operations.columns["max_concurrency"].nullable is False
    assert str(operations.columns["max_concurrency"].server_default.arg) == "1"


def test_cycle_liveness_control_model_defaults_match_existing_behavior():
    timeout = Cycle.__table__.c.timeout_seconds
    retry = Cycle.__table__.c.retry_policy
    concurrency = Cycle.__table__.c.max_concurrency

    assert timeout.nullable is True
    assert timeout.default is None
    assert retry.nullable is False
    assert retry.default.arg({}) == {}
    assert str(retry.server_default.arg) == "'{}'::jsonb"
    assert concurrency.nullable is False
    assert concurrency.default.arg == 1
    assert str(concurrency.server_default.arg) == "1"
