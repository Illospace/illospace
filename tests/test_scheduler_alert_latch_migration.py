"""Migration coverage for scheduler-global alert latches."""

from __future__ import annotations

import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
import sqlalchemy as sa


def test_scheduler_alert_latch_migration_round_trips(monkeypatch):
    migration = importlib.import_module(
        "brain.platform.db.alembic.versions.0054_scheduler_alert_latches"
    )
    assert migration.revision == "0054_scheduler_alert_latches"
    assert migration.down_revision == "0053_cycle_execution_policy_key"
    engine = sa.create_engine("sqlite://")

    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(migration, "op", operations)

        migration.upgrade()
        migration.upgrade()

        columns = {
            column["name"]: column
            for column in sa.inspect(connection).get_columns(
                "scheduler_alert_latches"
            )
        }
        assert set(columns) == {"alert_key", "alerted_at"}
        assert columns["alert_key"]["type"].length == 80
        assert columns["alerted_at"]["nullable"] is False

        migration.downgrade()
        migration.downgrade()
        assert "scheduler_alert_latches" not in sa.inspect(
            connection
        ).get_table_names()
