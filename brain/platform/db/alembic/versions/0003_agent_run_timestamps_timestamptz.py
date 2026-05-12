"""Store agent-run timestamps with timezone.

Revision ID: 0003_agent_run_timestamps_timestamptz
Revises: 0002_idea_timestamps_timestamptz
Create Date: 2026-05-12
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0003_agent_run_timestamps_timestamptz"
down_revision = "0002_idea_timestamps_timestamptz"
branch_labels = None
depends_on = None


_COLUMNS = {
    "agent_runs": (
        "created_at",
        "updated_at",
        "started_at",
        "paused_at",
        "completed_at",
        "failed_at",
        "canceled_at",
    ),
    "agent_run_events": ("created_at",),
    "agent_run_artifacts": ("created_at",),
}


def _column_data_type(table_name: str, column_name: str) -> str | None:
    bind = op.get_bind()
    return bind.execute(
        sa.text(
            """
            SELECT data_type
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = :table_name
              AND column_name = :column_name
            """
        ),
        {"table_name": table_name, "column_name": column_name},
    ).scalar_one_or_none()


def _alter_column(
    table_name: str,
    column_name: str,
    target_type: str,
    using_sql: str,
) -> None:
    op.execute(
        f'ALTER TABLE "{table_name}" '
        f'ALTER COLUMN "{column_name}" TYPE {target_type} '
        f'USING "{column_name}" {using_sql}'
    )


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    for table_name, column_names in _COLUMNS.items():
        for column_name in column_names:
            if _column_data_type(table_name, column_name) == "timestamp without time zone":
                _alter_column(
                    table_name,
                    column_name,
                    "TIMESTAMP WITH TIME ZONE",
                    "AT TIME ZONE 'UTC'",
                )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    for table_name, column_names in _COLUMNS.items():
        for column_name in column_names:
            if _column_data_type(table_name, column_name) == "timestamp with time zone":
                _alter_column(
                    table_name,
                    column_name,
                    "TIMESTAMP WITHOUT TIME ZONE",
                    "AT TIME ZONE 'UTC'",
                )
