"""Migration coverage for normalized scheduler failure-guard latches."""

from __future__ import annotations

import importlib

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def _schema_bytes(connection) -> bytes:
    rows = connection.execute(
        sa.text(
            "SELECT type, name, tbl_name, sql "
            "FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' "
            "ORDER BY type, name"
        )
    ).all()
    return repr([tuple(row) for row in rows]).encode()


def _row_bytes(connection) -> bytes:
    jobs = connection.execute(
        sa.text("SELECT * FROM scheduler_jobs ORDER BY id")
    ).all()
    latches = connection.execute(
        sa.text(
            "SELECT * FROM scheduler_failure_guard_latches "
            "ORDER BY job_id, trigger_kind"
        )
    ).all()
    return repr(
        {
            "scheduler_jobs": [tuple(row) for row in jobs],
            "scheduler_failure_guard_latches": [
                tuple(row) for row in latches
            ],
        }
    ).encode()


@pytest.mark.parametrize(
    ("failure_alerted_at", "rate_alerted_at", "expected_kinds"),
    [
        (
            "2026-07-27 12:00:00+00:00",
            None,
            ["consecutive"],
        ),
        (
            None,
            "2026-07-27 13:00:00+00:00",
            ["rolling_window"],
        ),
        (
            "2026-07-27 12:00:00+00:00",
            "2026-07-27 13:00:00+00:00",
            ["consecutive", "rolling_window"],
        ),
        (None, None, []),
    ],
    ids=["consecutive-only", "rolling-only", "both", "neither"],
)
def test_failure_guard_migration_restores_exact_prior_schema(
    monkeypatch,
    failure_alerted_at,
    rate_alerted_at,
    expected_kinds,
):
    migration = importlib.import_module(
        "brain.platform.db.alembic.versions.0045_scheduler_failure_guard_latches"
    )
    engine = sa.create_engine("sqlite://")

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
                "VALUES (1, :failure_alerted_at, :rate_alerted_at)"
            ),
            {
                "failure_alerted_at": failure_alerted_at,
                "rate_alerted_at": rate_alerted_at,
            },
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
        assert [row["trigger_kind"] for row in latches] == expected_kinds
        expected_timestamps = {
            "consecutive": failure_alerted_at,
            "rolling_window": rate_alerted_at,
        }
        assert {
            row["trigger_kind"]: row["alerted_at"] for row in latches
        } == {
            kind: expected_timestamps[kind]
            for kind in expected_kinds
        }

        migration.downgrade()

        restored = connection.execute(
            sa.text(
                "SELECT failure_alerted_at, rate_alerted_at "
                "FROM scheduler_jobs WHERE id = 1"
            )
        ).one()
        assert restored.failure_alerted_at == failure_alerted_at
        assert restored.rate_alerted_at == rate_alerted_at
        assert (
            "scheduler_failure_guard_latches"
            not in sa.inspect(connection).get_table_names()
        )


def test_failure_guard_downgrade_rejects_unrepresentable_trigger(monkeypatch):
    migration = importlib.import_module(
        "brain.platform.db.alembic.versions.0045_scheduler_failure_guard_latches"
    )
    engine = sa.create_engine("sqlite://")

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
                "VALUES (1, '2026-07-27 12:00:00+00:00', NULL)"
            )
        )
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(migration, "op", operations)
        migration.upgrade()

        connection.execute(
            sa.text(
                "INSERT INTO scheduler_failure_guard_latches "
                "(job_id, trigger_kind, alerted_at) "
                "VALUES (1, 'custom_trigger', '2026-07-27 14:00:00+00:00')"
            )
        )
        assert {
            column["name"]
            for column in sa.inspect(connection).get_columns("scheduler_jobs")
        } == {"id"}
        schema_before = _schema_bytes(connection)
        rows_before = _row_bytes(connection)

        with pytest.raises(
            RuntimeError,
            match="unrepresentable trigger kind: custom_trigger",
        ):
            migration.downgrade()

        assert _schema_bytes(connection) == schema_before
        assert _row_bytes(connection) == rows_before
