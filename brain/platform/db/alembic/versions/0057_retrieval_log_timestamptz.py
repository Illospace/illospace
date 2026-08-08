"""Normalize retrieval-log timestamps to timezone-aware UTC.

Revision ID: 0057_retrieval_log_timestamptz
Revises: 0056_scheduler_self_heal_attempts
Create Date: 2026-08-08
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0057_retrieval_log_timestamptz"
down_revision = "0056_scheduler_self_heal_attempts"
branch_labels = None
depends_on = None

_TABLE = "retrieval_log"
_COLUMN = "timestamp"


def _column_data_type() -> str | None:
    return op.get_bind().execute(
        sa.text(
            """
            SELECT data_type
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = :table_name
              AND column_name = :column_name
            """
        ),
        {"table_name": _TABLE, "column_name": _COLUMN},
    ).scalar_one_or_none()


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    if _column_data_type() == "timestamp without time zone":
        op.execute(
            'ALTER TABLE "retrieval_log" '
            'ALTER COLUMN "timestamp" TYPE TIMESTAMP WITH TIME ZONE '
            'USING "timestamp" AT TIME ZONE \'UTC\''
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    if _column_data_type() == "timestamp with time zone":
        op.execute(
            'ALTER TABLE "retrieval_log" '
            'ALTER COLUMN "timestamp" TYPE TIMESTAMP WITHOUT TIME ZONE '
            'USING "timestamp" AT TIME ZONE \'UTC\''
        )
