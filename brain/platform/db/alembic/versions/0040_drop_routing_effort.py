"""Drop the unused routing-decision effort column.

Revision ID: 0040_drop_routing_effort
Revises: 0039_clear_revision_model_pins
Create Date: 2026-07-24
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0040_drop_routing_effort"
down_revision = "0039_clear_revision_model_pins"
branch_labels = None
depends_on = None


def _inspector():
    return sa.inspect(op.get_bind())


def _schema() -> str | None:
    return "public" if op.get_bind().dialect.name == "postgresql" else None


def _table_exists(table_name: str) -> bool:
    return table_name in set(_inspector().get_table_names(schema=_schema()))


def _column_exists(table_name: str, column_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return any(
        column["name"] == column_name
        for column in _inspector().get_columns(table_name, schema=_schema())
    )


def upgrade() -> None:
    if _column_exists("routing_decisions", "selected_reasoning_effort"):
        op.drop_column("routing_decisions", "selected_reasoning_effort")


def downgrade() -> None:
    if _table_exists("routing_decisions") and not _column_exists(
        "routing_decisions",
        "selected_reasoning_effort",
    ):
        op.add_column(
            "routing_decisions",
            sa.Column("selected_reasoning_effort", sa.Text(), nullable=True),
        )
