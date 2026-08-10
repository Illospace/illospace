"""Migration coverage for normalized scheduler failure-guard latches."""

from __future__ import annotations

import importlib
import json

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


def test_trigger_state_migration_backfills_and_round_trips(monkeypatch):
    migration = importlib.import_module(
        "brain.platform.db.alembic.versions.0048_failure_guard_trigger_state"
    )
    engine = sa.create_engine("sqlite://")

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "CREATE TABLE scheduler_jobs ("
                "id INTEGER PRIMARY KEY, "
                "consecutive_failure_count INTEGER NOT NULL)"
            )
        )
        connection.execute(
            sa.text(
                "CREATE TABLE cycles ("
                "id INTEGER PRIMARY KEY, "
                "consecutive_failure_count INTEGER NOT NULL)"
            )
        )
        connection.execute(
            sa.text(
                "CREATE TABLE scheduler_failure_guard_latches ("
                "job_id INTEGER NOT NULL, "
                "trigger_kind VARCHAR(40) NOT NULL, "
                "alerted_at DATETIME NOT NULL, "
                "PRIMARY KEY (job_id, trigger_kind))"
            )
        )
        connection.execute(
            sa.text(
                "CREATE TABLE cycle_failure_guard_latches ("
                "cycle_id INTEGER NOT NULL, "
                "trigger_kind VARCHAR(40) NOT NULL, "
                "alerted_at DATETIME NOT NULL, "
                "PRIMARY KEY (cycle_id, trigger_kind))"
            )
        )
        # A prior interrupted invocation may have created schema without
        # backfilling data. Upgrade must treat those steps independently.
        connection.execute(
            sa.text(
                "CREATE TABLE scheduler_failure_guard_trigger_states ("
                "job_id INTEGER NOT NULL, "
                "trigger_kind VARCHAR(40) NOT NULL, "
                "trigger_state JSON NOT NULL DEFAULT '{}', "
                "PRIMARY KEY (job_id, trigger_kind))"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO scheduler_jobs "
                "(id, consecutive_failure_count) VALUES (1, 2)"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO cycles "
                "(id, consecutive_failure_count) VALUES (7, 1)"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO scheduler_failure_guard_latches "
                "(job_id, trigger_kind, alerted_at) VALUES "
                "(1, 'consecutive', '2026-07-28 12:00:00+00:00'), "
                "(1, 'rolling_window', '2026-07-28 13:00:00+00:00')"
            )
        )
        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(migration, "op", operations)

        migration.upgrade()
        migration.upgrade()

        for table_name in (
            "scheduler_failure_guard_latches",
            "cycle_failure_guard_latches",
        ):
            columns = {
                column["name"]: column
                for column in sa.inspect(connection).get_columns(table_name)
            }
            assert set(columns) == {
                (
                    "job_id"
                    if table_name.startswith("scheduler")
                    else "cycle_id"
                ),
                "trigger_kind",
                "alerted_at",
            }
            assert columns["alerted_at"]["nullable"] is False

        for owner_table in ("scheduler_jobs", "cycles"):
            assert "consecutive_failure_count" not in {
                column["name"]
                for column in sa.inspect(connection).get_columns(owner_table)
            }

        scheduler_rows = connection.execute(
            sa.text(
                "SELECT trigger_kind, trigger_state "
                "FROM scheduler_failure_guard_trigger_states "
                "ORDER BY trigger_kind"
            )
        ).mappings().all()
        assert [
            (
                row["trigger_kind"],
                json.loads(row["trigger_state"]),
            )
            for row in scheduler_rows
        ] == [
            (
                "consecutive",
                {"count": 2},
            ),
        ]
        cycle_row = connection.execute(
            sa.text(
                "SELECT trigger_kind, trigger_state "
                "FROM cycle_failure_guard_trigger_states"
            )
        ).mappings().one()
        assert cycle_row["trigger_kind"] == "consecutive"
        assert json.loads(cycle_row["trigger_state"]) == {"count": 1}
        assert connection.execute(
            sa.text(
                "SELECT trigger_kind, alerted_at "
                "FROM scheduler_failure_guard_latches "
                "ORDER BY trigger_kind"
            )
        ).all() == [
            ("consecutive", "2026-07-28 12:00:00+00:00"),
            ("rolling_window", "2026-07-28 13:00:00+00:00"),
        ]

        connection.execute(
            sa.text(
                "INSERT INTO scheduler_failure_guard_trigger_states "
                "(job_id, trigger_kind, trigger_state) "
                "VALUES (1, 'custom_state', :state)"
            ),
            {"state": json.dumps({"seen": ["alpha", "beta"]})},
        )

        schema_before_rejected_downgrade = _schema_bytes(connection)
        with pytest.raises(
            RuntimeError,
            match="unrepresentable trigger kind: custom_state",
        ):
            migration.downgrade()
        assert _schema_bytes(connection) == schema_before_rejected_downgrade

        connection.execute(
            sa.text(
                "DELETE FROM scheduler_failure_guard_trigger_states "
                "WHERE trigger_kind = 'custom_state'"
            )
        )
        migration.downgrade()
        migration.downgrade()

        assert {
            "scheduler_failure_guard_trigger_states",
            "cycle_failure_guard_trigger_states",
        }.isdisjoint(sa.inspect(connection).get_table_names())
        assert connection.execute(
            sa.text(
                "SELECT consecutive_failure_count "
                "FROM scheduler_jobs WHERE id = 1"
            )
        ).scalar_one() == 2
        assert connection.execute(
            sa.text(
                "SELECT consecutive_failure_count "
                "FROM cycles WHERE id = 7"
            )
        ).scalar_one() == 1
        assert connection.execute(
            sa.text(
                "SELECT trigger_kind FROM scheduler_failure_guard_latches "
                "ORDER BY trigger_kind"
            )
        ).scalars().all() == ["consecutive", "rolling_window"]
        assert connection.execute(
            sa.text("SELECT COUNT(*) FROM cycle_failure_guard_latches")
        ).scalar_one() == 0


def _orphan_cleanup_migration(monkeypatch, connection):
    migration = importlib.import_module(
        "brain.platform.db.alembic.versions."
        "0058_delete_orphaned_standing_failure_states"
    )
    monkeypatch.setattr(
        migration,
        "op",
        Operations(MigrationContext.configure(connection)),
    )
    return migration


def test_orphaned_standing_failure_state_is_deleted(monkeypatch):
    engine = sa.create_engine("sqlite://")

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "CREATE TABLE scheduler_failure_guard_trigger_states ("
                "job_id INTEGER NOT NULL, "
                "trigger_kind VARCHAR(40) NOT NULL, "
                "trigger_state BLOB NOT NULL, "
                "PRIMARY KEY (job_id, trigger_kind))"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO scheduler_failure_guard_trigger_states "
                "(job_id, trigger_kind, trigger_state) VALUES "
                "(1, 'standing_failure', :orphaned_state), "
                "(2, 'custom_state', :preserved_state)"
            ),
            {
                "orphaned_state": b'{"count": 9}',
                "preserved_state": b'{  "nested": [1, 2], "flag": true  }',
            },
        )
        preserved_before = connection.execute(
            sa.text(
                "SELECT job_id, trigger_kind, trigger_state "
                "FROM scheduler_failure_guard_trigger_states "
                "WHERE trigger_kind = 'custom_state'"
            )
        ).one()

        _orphan_cleanup_migration(monkeypatch, connection).upgrade()

        assert connection.execute(
            sa.text(
                "SELECT COUNT(*) "
                "FROM scheduler_failure_guard_trigger_states "
                "WHERE trigger_kind = 'standing_failure'"
            )
        ).scalar_one() == 0
        preserved_after = connection.execute(
            sa.text(
                "SELECT job_id, trigger_kind, trigger_state "
                "FROM scheduler_failure_guard_trigger_states "
                "WHERE trigger_kind = 'custom_state'"
            )
        ).one()
        assert tuple(preserved_after) == tuple(preserved_before)


def test_orphan_cleanup_is_noop_when_trigger_state_table_is_absent(monkeypatch):
    engine = sa.create_engine("sqlite://")

    with engine.begin() as connection:
        _orphan_cleanup_migration(monkeypatch, connection).upgrade()
