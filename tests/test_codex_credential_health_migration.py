"""Offline Alembic coverage for durable personal Codex connection health."""
import importlib

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


def test_credential_health_migration_handles_current_and_fresh_baselines():
    migration = importlib.import_module("brain.platform.db.alembic.versions.0069_codex_credential_health")
    assert migration.down_revision == "0068_reflex_missing_record_rule"
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE user_codex_connections (id INTEGER PRIMARY KEY)"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            migration.upgrade()
            columns = {c["name"] for c in inspect(connection).get_columns("user_codex_connections")}
            assert {"credential_error_code", "credential_error_at", "credential_alerted_at"} <= columns
            migration.downgrade()
        assert {c["name"] for c in inspect(connection).get_columns("user_codex_connections")} == {"id"}
    engine.dispose()
