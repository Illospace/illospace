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

_RUN_LOG_GENERATED_COLUMN_SQL = (
    "EXTRACT(EPOCH FROM (completed_at - runed_at))::INTEGER"
)


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


def _generated_column_expression(table_name: str, column_name: str) -> str | None:
    bind = op.get_bind()
    return bind.execute(
        sa.text(
            """
            SELECT generation_expression
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND table_name = :table_name
              AND column_name = :column_name
              AND is_generated = 'ALWAYS'
            """
        ),
        {"table_name": table_name, "column_name": column_name},
    ).scalar_one_or_none()


def _drop_run_log_duration_s() -> bool:
    if _generated_column_expression("run_log", "duration_s") is None:
        return False
    op.drop_column("run_log", "duration_s")
    return True


def _restore_run_log_duration_s() -> None:
    op.add_column(
        "run_log",
        sa.Column(
            "duration_s",
            sa.Integer(),
            sa.Computed(_RUN_LOG_GENERATED_COLUMN_SQL),
            nullable=True,
        ),
    )


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    dropped_run_log_duration_s = _drop_run_log_duration_s()
    for table_name, column_name in _target_columns("timestamp without time zone"):
        _alter_column(
            table_name,
            column_name,
            "TIMESTAMP WITH TIME ZONE",
            "AT TIME ZONE 'UTC'",
        )
    if dropped_run_log_duration_s:
        _restore_run_log_duration_s()


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    dropped_run_log_duration_s = _drop_run_log_duration_s()
    for table_name, column_name in _target_columns("timestamp with time zone"):
        _alter_column(
            table_name,
            column_name,
            "TIMESTAMP WITHOUT TIME ZONE",
            "AT TIME ZONE 'UTC'",
        )
    if dropped_run_log_duration_s:
        _restore_run_log_duration_s()
