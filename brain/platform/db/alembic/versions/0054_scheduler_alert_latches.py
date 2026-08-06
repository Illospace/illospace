"""Add durable scheduler-global alert latches.

Revision ID: 0054_scheduler_alert_latches
Revises: 0053_cycle_execution_policy_key
Create Date: 2026-08-06
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0054_scheduler_alert_latches"
down_revision = "0053_cycle_execution_policy_key"
branch_labels = None
depends_on = None

_TABLE = "scheduler_alert_latches"


def _schema() -> str | None:
    return "public" if op.get_bind().dialect.name == "postgresql" else None


def _table_exists() -> bool:
    return _TABLE in sa.inspect(op.get_bind()).get_table_names(schema=_schema())


def upgrade() -> None:
    if not _table_exists():
        op.create_table(
            _TABLE,
            sa.Column("alert_key", sa.String(length=80), nullable=False),
            sa.Column("alerted_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("alert_key"),
        )


def downgrade() -> None:
    if _table_exists():
        op.drop_table(_TABLE)
