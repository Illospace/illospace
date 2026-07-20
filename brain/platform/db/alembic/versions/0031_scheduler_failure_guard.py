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


def upgrade() -> None:
    op.add_column(
        "scheduler_jobs",
        sa.Column("failure_signature", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "scheduler_jobs",
        sa.Column(
            "consecutive_failure_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "scheduler_jobs",
        sa.Column("failure_alerted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "scheduler_jobs",
        sa.Column("last_failure_error", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("scheduler_jobs", "last_failure_error")
    op.drop_column("scheduler_jobs", "failure_alerted_at")
    op.drop_column("scheduler_jobs", "consecutive_failure_count")
    op.drop_column("scheduler_jobs", "failure_signature")
