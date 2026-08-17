"""Add the receipt-monitoring epoch to Cycle schedules.

Revision ID: 0064_cycle_receipt_monitoring
Revises: 0063_cycle_schedule_bindings
Create Date: 2026-08-17
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0064_cycle_receipt_monitoring"
down_revision = "0063_cycle_schedule_bindings"
branch_labels = None
depends_on = None

_TABLE = "cycles"
_COLUMN = "receipt_monitoring_started_at"


def _column_names() -> set[str]:
    return {
        str(column["name"])
        for column in sa.inspect(op.get_bind()).get_columns(
            _TABLE,
            schema="public" if op.get_bind().dialect.name == "postgresql" else None,
        )
    }


def upgrade() -> None:
    if _COLUMN not in _column_names():
        op.add_column(
            _TABLE,
            sa.Column(
                _COLUMN,
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.func.now(),
            ),
        )


def downgrade() -> None:
    if _COLUMN in _column_names():
        op.drop_column(_TABLE, _COLUMN)
