"""Add durable Cycle execution-policy keys.

Revision ID: 0052_cycle_execution_policy_key
Revises: 0051_open_ask_terminal_states
Create Date: 2026-08-05
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0052_cycle_execution_policy_key"
down_revision = "0051_open_ask_terminal_states"
branch_labels = None
depends_on = None

_POLICY_KEY = "uwear_backend_promotion_readiness"
_PROMOTION_CYCLE_NAME = "Uwear Backend Promotion Readiness"


def _schema() -> str | None:
    return "public" if op.get_bind().dialect.name == "postgresql" else None


def _columns(table: str) -> set[str]:
    return {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns(
            table,
            schema=_schema(),
        )
    }


def upgrade() -> None:
    if "execution_policy_key" not in _columns("cycles"):
        op.add_column(
            "cycles",
            sa.Column("execution_policy_key", sa.String(length=100), nullable=True),
        )
    if "execution_policy_key" not in _columns("cycle_revisions"):
        op.add_column(
            "cycle_revisions",
            sa.Column("execution_policy_key", sa.String(length=100), nullable=True),
        )

    op.execute(
        sa.text(
            "UPDATE cycles "
            "SET execution_policy_key = :policy_key "
            "WHERE name = :cycle_name AND execution_policy_key IS NULL"
        ).bindparams(
            policy_key=_POLICY_KEY,
            cycle_name=_PROMOTION_CYCLE_NAME,
        )
    )
    op.execute(
        sa.text(
            "UPDATE cycle_revisions "
            "SET execution_policy_key = :policy_key "
            "WHERE cycle_id IN ("
            "SELECT id FROM cycles WHERE execution_policy_key = :policy_key"
            ") AND execution_policy_key IS NULL"
        ).bindparams(policy_key=_POLICY_KEY)
    )


def downgrade() -> None:
    if "execution_policy_key" in _columns("cycle_revisions"):
        op.drop_column("cycle_revisions", "execution_policy_key")
    if "execution_policy_key" in _columns("cycles"):
        op.drop_column("cycles", "execution_policy_key")
