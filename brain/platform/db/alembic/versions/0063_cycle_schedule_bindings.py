"""Add executor and skill bindings to Cycle schedules.

Revision ID: 0063_cycle_schedule_bindings
Revises: 0062_cycle_behavior_change_audits
Create Date: 2026-08-14
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0063_cycle_schedule_bindings"
down_revision = "0062_cycle_behavior_change_audits"
branch_labels = None
depends_on = None

_TABLE = "cycles"
_EXECUTOR_CONSTRAINT = "ck_cycles_executor_binding"


def _column_names() -> set[str]:
    return {
        str(column["name"])
        for column in sa.inspect(op.get_bind()).get_columns(
            _TABLE,
            schema="public" if op.get_bind().dialect.name == "postgresql" else None,
        )
    }


def _check_constraint_names() -> set[str]:
    return {
        str(constraint["name"])
        for constraint in sa.inspect(op.get_bind()).get_check_constraints(
            _TABLE,
            schema="public" if op.get_bind().dialect.name == "postgresql" else None,
        )
        if constraint.get("name")
    }


def _json_type():
    if op.get_bind().dialect.name == "postgresql":
        return postgresql.JSONB(astext_type=sa.Text())
    return sa.JSON()


def _empty_list_default():
    if op.get_bind().dialect.name == "postgresql":
        return sa.text("'[]'::jsonb")
    return sa.text("'[]'")


def _create_executor_constraint() -> None:
    condition = "executor_binding IN ('illo-lane', 'personal-agent')"
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table(_TABLE) as batch:
            batch.create_check_constraint(_EXECUTOR_CONSTRAINT, condition)
        return
    op.create_check_constraint(_EXECUTOR_CONSTRAINT, _TABLE, condition)


def _drop_executor_constraint() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table(_TABLE) as batch:
            batch.drop_constraint(_EXECUTOR_CONSTRAINT, type_="check")
        return
    op.drop_constraint(_EXECUTOR_CONSTRAINT, _TABLE, type_="check")


def upgrade() -> None:
    columns = _column_names()
    if "executor_binding" not in columns:
        op.add_column(
            _TABLE,
            sa.Column(
                "executor_binding",
                sa.String(length=32),
                nullable=False,
                server_default=sa.text("'illo-lane'"),
            ),
        )
    if "skill_ids" not in columns:
        op.add_column(
            _TABLE,
            sa.Column(
                "skill_ids",
                _json_type(),
                nullable=False,
                server_default=_empty_list_default(),
            ),
        )
    if _EXECUTOR_CONSTRAINT not in _check_constraint_names():
        _create_executor_constraint()


def downgrade() -> None:
    columns = _column_names()
    if _EXECUTOR_CONSTRAINT in _check_constraint_names():
        _drop_executor_constraint()
    if "skill_ids" in columns:
        op.drop_column(_TABLE, "skill_ids")
    if "executor_binding" in columns:
        op.drop_column(_TABLE, "executor_binding")
