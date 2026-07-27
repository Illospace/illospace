"""Add Cycle liveness control fields.

Revision ID: 0043_cycle_liveness_controls
Revises: 0042_scheduler_failure_rate_guard
Create Date: 2026-07-27
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0043_cycle_liveness_controls"
down_revision = "0042_scheduler_failure_rate_guard"
branch_labels = None
depends_on = None


def _schema() -> str | None:
    return "public" if op.get_bind().dialect.name == "postgresql" else None


def _column_exists(column_name: str) -> bool:
    return column_name in {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns(
            "cycles",
            schema=_schema(),
        )
    }


def _json_type():
    if op.get_bind().dialect.name == "postgresql":
        return postgresql.JSONB(astext_type=sa.Text())
    return sa.JSON()


def _empty_json_default():
    if op.get_bind().dialect.name == "postgresql":
        return sa.text("'{}'::jsonb")
    return sa.text("'{}'")


def upgrade() -> None:
    if not _column_exists("max_concurrency"):
        op.add_column(
            "cycles",
            sa.Column(
                "max_concurrency",
                sa.Integer(),
                server_default=sa.text("1"),
                nullable=False,
            ),
        )
    if not _column_exists("timeout_seconds"):
        op.add_column(
            "cycles",
            sa.Column("timeout_seconds", sa.Integer(), nullable=True),
        )
    if not _column_exists("retry_policy"):
        op.add_column(
            "cycles",
            sa.Column(
                "retry_policy",
                _json_type(),
                server_default=_empty_json_default(),
                nullable=False,
            ),
        )


def downgrade() -> None:
    if _column_exists("retry_policy"):
        op.drop_column("cycles", "retry_policy")
    if _column_exists("timeout_seconds"):
        op.drop_column("cycles", "timeout_seconds")
    if _column_exists("max_concurrency"):
        op.drop_column("cycles", "max_concurrency")
