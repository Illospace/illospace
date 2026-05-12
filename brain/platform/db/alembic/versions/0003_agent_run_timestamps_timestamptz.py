"""Normalize database timestamps with timezone.

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


def _target_columns(source_data_type: str) -> list[tuple[str, str]]:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            """
            SELECT columns.table_name, columns.column_name
            FROM information_schema.columns AS columns
            JOIN information_schema.tables AS tables
              ON tables.table_schema = columns.table_schema
             AND tables.table_name = columns.table_name
            WHERE columns.table_schema = current_schema()
              AND columns.data_type = :source_data_type
              AND tables.table_type = 'BASE TABLE'
            ORDER BY columns.table_name, columns.column_name
            """
        ),
        {"source_data_type": source_data_type},
    )
    return [(str(row.table_name), str(row.column_name)) for row in rows]


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

    for table_name, column_name in _target_columns("timestamp without time zone"):
        _alter_column(
            table_name,
            column_name,
            "TIMESTAMP WITH TIME ZONE",
            "AT TIME ZONE 'UTC'",
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    for table_name, column_name in _target_columns("timestamp with time zone"):
        _alter_column(
            table_name,
            column_name,
            "TIMESTAMP WITHOUT TIME ZONE",
            "AT TIME ZONE 'UTC'",
        )
