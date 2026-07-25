"""Persist scheduler rolling failure-rate alert state.

Revision ID: 0042_scheduler_failure_rate_guard
Revises: 0041_agent_api_call_effort
Create Date: 2026-07-25
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0042_scheduler_failure_rate_guard"
down_revision = "0041_agent_api_call_effort"
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
    if not _column_exists("rate_alerted_at"):
        op.add_column(
            "scheduler_jobs",
            sa.Column("rate_alerted_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    if _column_exists("rate_alerted_at"):
        op.drop_column("scheduler_jobs", "rate_alerted_at")
