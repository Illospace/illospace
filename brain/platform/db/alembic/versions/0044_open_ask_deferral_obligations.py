"""Extend the open-ask ledger to carry run deferral obligations.

Revision ID: 0044_open_ask_deferral_obligations
Revises: 0042_scheduler_failure_rate_guard
Create Date: 2026-07-27
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0044_open_ask_deferral_obligations"
down_revision = "0042_scheduler_failure_rate_guard"
branch_labels = None
depends_on = None

_TABLE = "open_asks"
_DEFERRAL_INDEX = "uq_open_asks_run_deferral"
_KIND_CONSTRAINT = "ck_open_asks_obligation_kind"


def _schema() -> str | None:
    return "public" if op.get_bind().dialect.name == "postgresql" else None


def _inspector():
    return sa.inspect(op.get_bind())


def _column_exists(column_name: str) -> bool:
    return column_name in {
        column["name"]
        for column in _inspector().get_columns(_TABLE, schema=_schema())
    }


def _index_exists(index_name: str) -> bool:
    return index_name in {
        index["name"]
        for index in _inspector().get_indexes(_TABLE, schema=_schema())
    }


def _constraint_exists(constraint_name: str) -> bool:
    return constraint_name in {
        constraint["name"]
        for constraint in _inspector().get_check_constraints(
            _TABLE,
            schema=_schema(),
        )
    }


def upgrade() -> None:
    if not _column_exists("obligation_kind"):
        op.add_column(
            _TABLE,
            sa.Column(
                "obligation_kind",
                sa.String(length=32),
                server_default=sa.text("'human_ask'"),
                nullable=False,
            ),
        )
    if not _column_exists("notice_conditions"):
        op.add_column(
            _TABLE,
            sa.Column("notice_conditions", sa.Text(), nullable=True),
        )
    if (
        op.get_bind().dialect.name == "postgresql"
        and not _constraint_exists(_KIND_CONSTRAINT)
    ):
        op.create_check_constraint(
            _KIND_CONSTRAINT,
            _TABLE,
            "obligation_kind IN ('human_ask', 'run_deferral')",
        )
    if not _index_exists(_DEFERRAL_INDEX):
        op.create_index(
            _DEFERRAL_INDEX,
            _TABLE,
            ["org_id", "channel_id", "thread_ts", "origin_run_id"],
            unique=True,
            postgresql_where=sa.text("obligation_kind = 'run_deferral'"),
            sqlite_where=sa.text("obligation_kind = 'run_deferral'"),
        )


def downgrade() -> None:
    # Preserve unresolved obligations and their notification history.
    return None
