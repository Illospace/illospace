"""Add an attempt count to scheduler alert latches.

Revision ID: 0056_scheduler_self_heal_attempts
Revises: 0055_scheduler_alert_escalation
Create Date: 2026-08-07
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0056_scheduler_self_heal_attempts"
down_revision = "0055_scheduler_alert_escalation"
branch_labels = None
depends_on = None

_TABLE = "scheduler_alert_latches"
_COLUMN = "attempt_count"


def _schema() -> str | None:
    return "public" if op.get_bind().dialect.name == "postgresql" else None


def _columns() -> set[str]:
    return {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns(
            _TABLE,
            schema=_schema(),
        )
    }


def upgrade() -> None:
    if _COLUMN not in _columns():
        op.add_column(
            _TABLE,
            sa.Column(
                _COLUMN,
                sa.Integer(),
                nullable=False,
                server_default="0",
            ),
        )


def downgrade() -> None:
    if _COLUMN in _columns():
        op.drop_column(_TABLE, _COLUMN)
