"""Persist personal Codex credential expiry and its alert receipt.

Revision ID: 0069_codex_credential_health
Revises: 0068_reflex_missing_record_rule
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0069_codex_credential_health"
down_revision = "0068_reflex_missing_record_rule"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("user_codex_connections")
    }
    for column in (
        sa.Column("credential_error_code", sa.String(50), nullable=True),
        sa.Column("credential_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("credential_alerted_at", sa.DateTime(timezone=True), nullable=True),
    ):
        if column.name not in existing:
            op.add_column("user_codex_connections", column)


def downgrade() -> None:
    existing = {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns("user_codex_connections")
    }
    for column in ("credential_alerted_at", "credential_error_at", "credential_error_code"):
        if column in existing:
            op.drop_column("user_codex_connections", column)
