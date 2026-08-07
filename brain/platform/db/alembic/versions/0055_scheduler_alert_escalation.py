"""Add duration state to scheduler alert latches.

Revision ID: 0055_scheduler_alert_escalation
Revises: 0054_scheduler_alert_latches
Create Date: 2026-08-07
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0055_scheduler_alert_escalation"
down_revision = "0054_scheduler_alert_latches"
branch_labels = None
depends_on = None

_TABLE = "scheduler_alert_latches"
_COLUMNS = ("freeze_started_at", "next_alert_at")


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
    columns = _columns()
    for name in _COLUMNS:
        if name not in columns:
            op.add_column(
                _TABLE,
                sa.Column(name, sa.DateTime(timezone=True), nullable=True),
            )


def downgrade() -> None:
    columns = _columns()
    for name in reversed(_COLUMNS):
        if name in columns:
            op.drop_column(_TABLE, name)
