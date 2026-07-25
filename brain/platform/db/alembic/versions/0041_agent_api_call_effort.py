"""Record the canonical effort tier on each model API call.

Historical rows remain NULL. New telemetry rows persist the run's canonical
effort tier so token and cost reporting can be broken down by routing tier.

Revision ID: 0041_agent_api_call_effort
Revises: 0040_drop_routing_effort
Create Date: 2026-07-24
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0041_agent_api_call_effort"
down_revision = "0040_drop_routing_effort"
branch_labels = None
depends_on = None


def _column_exists(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    schema = "public" if op.get_bind().dialect.name == "postgresql" else None
    return column_name in {
        column["name"]
        for column in inspector.get_columns(table_name, schema=schema)
    }


def upgrade() -> None:
    if not _column_exists("agent_api_calls", "effort"):
        op.add_column(
            "agent_api_calls",
            sa.Column("effort", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    if _column_exists("agent_api_calls", "effort"):
        op.drop_column("agent_api_calls", "effort")
