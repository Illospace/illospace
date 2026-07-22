"""Persist scheduler repeated-failure guard state.

Revision ID: 0031_scheduler_failure_guard
Revises: 0030_consolidation_phase_width
Create Date: 2026-07-20
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0031_scheduler_failure_guard"
down_revision = "0030_consolidation_phase_width"
branch_labels = None
depends_on = None


def _schema() -> str | None:
    return "public" if op.get_bind().dialect.name == "postgresql" else None


def _column_exists(column_name: str) -> bool:
    return column_name in {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns(
            "scheduler_jobs",
            schema=_schema(),
        )
    }


def upgrade() -> None:
    if not _column_exists("failure_signature"):
        op.add_column(
            "scheduler_jobs",
            sa.Column("failure_signature", sa.String(length=64), nullable=True),
        )
    if not _column_exists("consecutive_failure_count"):
        op.add_column(
            "scheduler_jobs",
            sa.Column(
                "consecutive_failure_count",
                sa.Integer(),
                server_default=sa.text("0"),
                nullable=False,
            ),
        )
    if not _column_exists("failure_alerted_at"):
        op.add_column(
            "scheduler_jobs",
            sa.Column("failure_alerted_at", sa.DateTime(timezone=True), nullable=True),
        )
    if not _column_exists("last_failure_error"):
        op.add_column(
            "scheduler_jobs",
            sa.Column("last_failure_error", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    if _column_exists("last_failure_error"):
        op.drop_column("scheduler_jobs", "last_failure_error")
    if _column_exists("failure_alerted_at"):
        op.drop_column("scheduler_jobs", "failure_alerted_at")
    if _column_exists("consecutive_failure_count"):
        op.drop_column("scheduler_jobs", "consecutive_failure_count")
    if _column_exists("failure_signature"):
        op.drop_column("scheduler_jobs", "failure_signature")
