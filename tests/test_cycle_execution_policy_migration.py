"""Migration coverage for durable Cycle execution-policy keys."""

from __future__ import annotations

import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
import sqlalchemy as sa


@pytest.mark.parametrize("promotion_cycle_present", [False, True])
def test_cycle_execution_policy_key_migration_backfills_safely(
    monkeypatch,
    promotion_cycle_present,
):
    migration = importlib.import_module(
        "brain.platform.db.alembic.versions.0052_cycle_execution_policy_key"
    )
    assert migration.revision == "0052_cycle_execution_policy_key"
    assert migration.down_revision == "0051_open_ask_terminal_states"
    engine = sa.create_engine("sqlite://")

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "CREATE TABLE cycles ("
                "id INTEGER PRIMARY KEY, "
                "name TEXT NOT NULL)"
            )
        )
        connection.execute(
            sa.text(
                "CREATE TABLE cycle_revisions ("
                "id INTEGER PRIMARY KEY, "
                "cycle_id INTEGER NOT NULL, "
                "name TEXT NOT NULL)"
            )
        )
        connection.execute(
            sa.text("INSERT INTO cycles (id, name) VALUES (8, 'Other cycle')")
        )
        connection.execute(
            sa.text(
                "INSERT INTO cycle_revisions (id, cycle_id, name) "
                "VALUES (80, 8, 'Other cycle')"
            )
        )
        if promotion_cycle_present:
            connection.execute(
                sa.text(
                    "INSERT INTO cycles (id, name) VALUES "
                    "(9, 'Uwear Backend Promotion Readiness')"
                )
            )
            connection.execute(
                sa.text(
                    "INSERT INTO cycle_revisions (id, cycle_id, name) "
                    "VALUES (90, 9, 'Uwear Backend Promotion Readiness')"
                )
            )

        operations = Operations(MigrationContext.configure(connection))
        monkeypatch.setattr(migration, "op", operations)

        migration.upgrade()
        migration.upgrade()

        cycle_rows = connection.execute(
            sa.text(
                "SELECT id, execution_policy_key FROM cycles ORDER BY id"
            )
        ).all()
        revision_rows = connection.execute(
            sa.text(
                "SELECT cycle_id, execution_policy_key "
                "FROM cycle_revisions ORDER BY cycle_id"
            )
        ).all()
        expected_policy_key = "uwear_backend_promotion_readiness"
        assert cycle_rows == (
            [(8, None), (9, expected_policy_key)]
            if promotion_cycle_present
            else [(8, None)]
        )
        assert revision_rows == (
            [(8, None), (9, expected_policy_key)]
            if promotion_cycle_present
            else [(8, None)]
        )

        migration.downgrade()
        migration.downgrade()
        assert "execution_policy_key" not in {
            column["name"]
            for column in sa.inspect(connection).get_columns("cycles")
        }
        assert "execution_policy_key" not in {
            column["name"]
            for column in sa.inspect(connection).get_columns("cycle_revisions")
        }
