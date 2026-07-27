"""Migration coverage for normalized scheduler failure-guard latches."""

from __future__ import annotations

import importlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_failure_guard_migration_preserves_both_existing_latches(monkeypatch):
    migration = importlib.import_module(
        "brain.platform.db.alembic.versions.0045_scheduler_failure_guard_latches"
    )
    engine = sa.create_engine("sqlite://")
    alerted_at = "2026-07-27 12:00:00+00:00"

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "CREATE TABLE scheduler_jobs ("
                "id INTEGER PRIMARY KEY, "
                "failure_alerted_at DATETIME, "
                "rate_alerted_at DATETIME)"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO scheduler_jobs "
                "(id, failure_alerted_at, rate_alerted_at) "
                "VALUES (1, :alerted_at, :alerted_at), (2, NULL, NULL)"
            ),
            {"alerted_at": alerted_at},
        )
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(migration, "op", operations)

        migration.upgrade()
        migration.upgrade()

        job_columns = {
            column["name"]
            for column in sa.inspect(connection).get_columns("scheduler_jobs")
        }
        assert "failure_alerted_at" not in job_columns
        assert "rate_alerted_at" not in job_columns
        latches = connection.execute(
            sa.text(
                "SELECT job_id, trigger_kind, alerted_at "
                "FROM scheduler_failure_guard_latches "
                "ORDER BY trigger_kind"
            )
        ).mappings().all()
        assert [(row["job_id"], row["trigger_kind"]) for row in latches] == [
            (1, "consecutive"),
            (1, "rolling_window"),
        ]
        assert all(row["alerted_at"] is not None for row in latches)

        migration.downgrade()

        restored = connection.execute(
            sa.text(
                "SELECT failure_alerted_at, rate_alerted_at "
                "FROM scheduler_jobs WHERE id = 1"
            )
        ).one()
        assert restored.failure_alerted_at is not None
        assert restored.rate_alerted_at is not None
