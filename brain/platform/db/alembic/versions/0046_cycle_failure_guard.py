"""Persist normalized cycle failure-guard state and run observations.

Revision ID: 0046_cycle_failure_guard
Revises: 0045_scheduler_failure_guard_latches
Create Date: 2026-07-28
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0046_cycle_failure_guard"
down_revision = "0045_scheduler_failure_guard_latches"
branch_labels = None
depends_on = None

_CYCLE_TABLE = "cycles"
_LATCH_TABLE = "cycle_failure_guard_latches"
_OBSERVATION_TABLE = "cycle_failure_guard_observations"


def _schema() -> str | None:
    return "public" if op.get_bind().dialect.name == "postgresql" else None


def _column_exists(column_name: str) -> bool:
    return column_name in {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns(
            _CYCLE_TABLE,
            schema=_schema(),
        )
    }


def _table_exists(table_name: str) -> bool:
    return table_name in sa.inspect(op.get_bind()).get_table_names(
        schema=_schema(),
    )


def upgrade() -> None:
    if not _column_exists("failure_signature"):
        op.add_column(
            _CYCLE_TABLE,
            sa.Column("failure_signature", sa.String(length=64), nullable=True),
        )
    if not _column_exists("consecutive_failure_count"):
        op.add_column(
            _CYCLE_TABLE,
            sa.Column(
                "consecutive_failure_count",
                sa.Integer(),
                server_default=sa.text("0"),
                nullable=False,
            ),
        )
    if not _column_exists("last_failure_error"):
        op.add_column(
            _CYCLE_TABLE,
            sa.Column("last_failure_error", sa.Text(), nullable=True),
        )

    if not _table_exists(_LATCH_TABLE):
        op.create_table(
            _LATCH_TABLE,
            sa.Column(
                "cycle_id",
                sa.Integer(),
                sa.ForeignKey("cycles.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("trigger_kind", sa.String(length=40), primary_key=True),
            sa.Column(
                "alerted_at",
                sa.DateTime(timezone=True),
                nullable=False,
            ),
        )
    if not _table_exists(_OBSERVATION_TABLE):
        op.create_table(
            _OBSERVATION_TABLE,
            sa.Column(
                "cycle_run_id",
                sa.Integer(),
                sa.ForeignKey("cycle_runs.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column(
                "observed_at",
                sa.DateTime(timezone=True),
                nullable=False,
            ),
        )


def downgrade() -> None:
    if _table_exists(_OBSERVATION_TABLE):
        op.drop_table(_OBSERVATION_TABLE)
    if _table_exists(_LATCH_TABLE):
        op.drop_table(_LATCH_TABLE)
    if _column_exists("last_failure_error"):
        op.drop_column(_CYCLE_TABLE, "last_failure_error")
    if _column_exists("consecutive_failure_count"):
        op.drop_column(_CYCLE_TABLE, "consecutive_failure_count")
    if _column_exists("failure_signature"):
        op.drop_column(_CYCLE_TABLE, "failure_signature")
