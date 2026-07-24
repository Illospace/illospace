"""Add shared per-Cycle exception-ping throttle state.

Revision ID: 0037_exception_ping_state
Revises: 0036_chantier_members_blockers
Create Date: 2026-07-24
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0037_exception_ping_state"
down_revision = "0036_chantier_members_blockers"
branch_labels = None
depends_on = None

_TABLE = "cycles"
_COLUMN = "exception_ping_state"


def _schema() -> str | None:
    return "public" if op.get_bind().dialect.name == "postgresql" else None


def _column_exists() -> bool:
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in set(inspector.get_table_names(schema=_schema())):
        return False
    return _COLUMN in {
        column["name"]
        for column in inspector.get_columns(_TABLE, schema=_schema())
    }


def _json_type():
    if op.get_bind().dialect.name == "postgresql":
        return postgresql.JSONB(astext_type=sa.Text())
    return sa.JSON()


def _empty_json_default():
    if op.get_bind().dialect.name == "postgresql":
        return sa.text("'{}'::jsonb")
    return sa.text("'{}'")


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in set(inspector.get_table_names(schema=_schema())):
        return
    if not _column_exists():
        op.add_column(
            _TABLE,
            sa.Column(
                _COLUMN,
                _json_type(),
                server_default=_empty_json_default(),
                nullable=False,
            ),
        )


def downgrade() -> None:
    # Preserve the throttle ledger: dropping it would immediately permit a
    # duplicate person-addressed ping after a deploy rollback.
    return None
