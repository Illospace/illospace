"""Add source idempotency keys to agent runs.

Revision ID: 0015_agent_run_source_idempotency
Revises: 0014_backfill_domain_write_scope
Create Date: 2026-06-01
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0015_agent_run_source_idempotency"
down_revision = "0014_backfill_domain_write_scope"
branch_labels = None
depends_on = None


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _table_exists(table_name: str) -> bool:
    return table_name in set(_inspector().get_table_names(schema="public"))


def _column_exists(table_name: str, column_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return column_name in {
        column["name"]
        for column in _inspector().get_columns(table_name, schema="public")
    }


def _constraint_exists(table_name: str, constraint_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    constraints = _inspector().get_unique_constraints(table_name, schema="public")
    constraints += _inspector().get_foreign_keys(table_name, schema="public")
    return constraint_name in {constraint["name"] for constraint in constraints}


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if _table_exists(table_name) and not _column_exists(table_name, str(column.name)):
        op.add_column(table_name, column)


def _create_unique_if_missing(table_name: str, constraint_name: str, columns: list[str]) -> None:
    if (
        _table_exists(table_name)
        and all(_column_exists(table_name, column) for column in columns)
        and not _constraint_exists(table_name, constraint_name)
    ):
        op.create_unique_constraint(constraint_name, table_name, columns)


def _drop_constraint_if_exists(table_name: str, constraint_name: str, type_: str) -> None:
    if _constraint_exists(table_name, constraint_name):
        op.drop_constraint(constraint_name, table_name, type_=type_)


def _drop_column_if_exists(table_name: str, column_name: str) -> None:
    if _column_exists(table_name, column_name):
        op.drop_column(table_name, column_name)


def upgrade() -> None:
    _add_column_if_missing("agent_runs", sa.Column("source_idempotency_scope", sa.String(length=80), nullable=True))
    _add_column_if_missing("agent_runs", sa.Column("source_idempotency_key", sa.Text(), nullable=True))
    _create_unique_if_missing(
        "agent_runs",
        "uq_agent_runs_org_source_idempotency",
        ["org_id", "source_idempotency_scope", "source_idempotency_key"],
    )


def downgrade() -> None:
    _drop_constraint_if_exists("agent_runs", "uq_agent_runs_org_source_idempotency", type_="unique")
    _drop_column_if_exists("agent_runs", "source_idempotency_key")
    _drop_column_if_exists("agent_runs", "source_idempotency_scope")
