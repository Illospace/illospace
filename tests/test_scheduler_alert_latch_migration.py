"""Migration coverage for scheduler-global, duration-aware alert latches."""

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


def test_scheduler_alert_escalation_migration_preserves_live_rows(monkeypatch):
    latch_migration = importlib.import_module(
        "brain.platform.db.alembic.versions.0054_scheduler_alert_latches"
    )
    escalation_migration = importlib.import_module(
        "brain.platform.db.alembic.versions.0055_scheduler_alert_escalation"
    )
    assert escalation_migration.revision == "0055_scheduler_alert_escalation"
    assert escalation_migration.down_revision == "0054_scheduler_alert_latches"
    engine = sa.create_engine("sqlite://")

    with engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(latch_migration, "op", operations)
        monkeypatch.setattr(escalation_migration, "op", operations)

        latch_migration.upgrade()
        connection.exec_driver_sql(
            "INSERT INTO scheduler_alert_latches (alert_key, alerted_at) "
            "VALUES (?, ?)",
            (
                "scheduler_overdue_freeze",
                "2026-08-07 04:05:04+00:00",
            ),
        )

        escalation_migration.upgrade()
        escalation_migration.upgrade()

        columns = {
            column["name"]: column
            for column in sa.inspect(connection).get_columns(
                "scheduler_alert_latches"
            )
        }
        assert set(columns) == {
            "alert_key",
            "alerted_at",
            "freeze_started_at",
            "next_alert_at",
        }
        assert columns["freeze_started_at"]["nullable"] is True
        assert columns["next_alert_at"]["nullable"] is True
        assert connection.exec_driver_sql(
            "SELECT alert_key, freeze_started_at, next_alert_at "
            "FROM scheduler_alert_latches"
        ).one() == ("scheduler_overdue_freeze", None, None)

        escalation_migration.downgrade()
        escalation_migration.downgrade()

        assert {
            column["name"]
            for column in sa.inspect(connection).get_columns(
                "scheduler_alert_latches"
            )
        } == {"alert_key", "alerted_at"}
        assert connection.exec_driver_sql(
            "SELECT alert_key FROM scheduler_alert_latches"
        ).scalar_one() == "scheduler_overdue_freeze"
