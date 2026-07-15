"""Add durable per-Cycle degradation state.

Revision ID: 0024_cycle_degradation_state
Revises: 0023_agent_run_parent_step_idempotency
Create Date: 2026-07-15
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0024_cycle_degradation_state"
down_revision = "0023_agent_run_parent_step_idempotency"
branch_labels = None
depends_on = None


def _schema() -> str | None:
    return "public" if op.get_bind().dialect.name == "postgresql" else None


def _column_exists(column_name: str) -> bool:
    return column_name in {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("cycles", schema=_schema())
    }


def upgrade() -> None:
    if _column_exists("degradation_state"):
        return
    json_type = sa.JSON() if op.get_bind().dialect.name == "sqlite" else JSONB()
    op.add_column(
        "cycles",
        sa.Column(
            "degradation_state",
            json_type,
            nullable=False,
            server_default=sa.text(
                "'{}'" if op.get_bind().dialect.name == "sqlite" else "'{}'::jsonb"
            ),
        ),
    )


def downgrade() -> None:
    if _column_exists("degradation_state"):
        op.drop_column("cycles", "degradation_state")
