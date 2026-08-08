"""Regression coverage for retrieval-log timestamp normalization."""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from unittest.mock import MagicMock


def _migration_with_type(monkeypatch, data_type: str):
    migration = importlib.import_module(
        "brain.platform.db.alembic.versions.0057_retrieval_log_timestamptz"
    )
    result = MagicMock()
    result.scalar_one_or_none.return_value = data_type
    bind = SimpleNamespace(
        dialect=SimpleNamespace(name="postgresql"),
        execute=MagicMock(return_value=result),
    )
    operation = MagicMock()
    operation.get_bind.return_value = bind
    monkeypatch.setattr(migration, "op", operation)
    return migration, operation


def test_upgrade_interprets_legacy_naive_values_as_utc(monkeypatch):
    migration, operation = _migration_with_type(
        monkeypatch,
        "timestamp without time zone",
    )

    migration.upgrade()

    statement = operation.execute.call_args.args[0]
    assert "TYPE TIMESTAMP WITH TIME ZONE" in statement
    assert "USING \"timestamp\" AT TIME ZONE 'UTC'" in statement


def test_upgrade_is_noop_when_column_is_already_timezone_aware(monkeypatch):
    migration, operation = _migration_with_type(
        monkeypatch,
        "timestamp with time zone",
    )

    migration.upgrade()

    operation.execute.assert_not_called()


def test_downgrade_preserves_absolute_time_as_utc_naive(monkeypatch):
    migration, operation = _migration_with_type(
        monkeypatch,
        "timestamp with time zone",
    )

    migration.downgrade()

    statement = operation.execute.call_args.args[0]
    assert "TYPE TIMESTAMP WITHOUT TIME ZONE" in statement
    assert "USING \"timestamp\" AT TIME ZONE 'UTC'" in statement
