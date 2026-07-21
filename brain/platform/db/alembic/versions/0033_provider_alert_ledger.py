"""Add durable provider-alert signature acknowledgement ledger.

Revision ID: 0033_provider_alert_ledger
Revises: 0031_scheduler_failure_guard, 0032_chantier_superseded_by
Create Date: 2026-07-21
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0033_provider_alert_ledger"
down_revision = (
    "0031_scheduler_failure_guard",
    "0032_chantier_superseded_by",
)
branch_labels = None
depends_on = None

_TABLE = "provider_alert_ledger"


def _schema() -> str | None:
    return "public" if op.get_bind().dialect.name == "postgresql" else None


def _inspector():
    return sa.inspect(op.get_bind())


def _table_exists(table_name: str) -> bool:
    return table_name in set(_inspector().get_table_names(schema=_schema()))


def _index_exists(table_name: str, index_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return index_name in {
        index["name"]
        for index in _inspector().get_indexes(table_name, schema=_schema())
    }


def _uuid_type():
    if op.get_bind().dialect.name == "postgresql":
        return postgresql.UUID(as_uuid=False)
    return sa.String()


def upgrade() -> None:
    if not _table_exists(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("org_id", _uuid_type(), nullable=False),
            sa.Column("channel_id", sa.String(length=80), nullable=False),
            sa.Column("signature", sa.String(length=64), nullable=False),
            sa.Column("classification", sa.String(length=80), nullable=False),
            sa.Column("severity", sa.String(length=20), nullable=False),
            sa.Column("rule_id", sa.String(length=120), nullable=False),
            sa.Column(
                "occurrence_count",
                sa.Integer(),
                server_default=sa.text("1"),
                nullable=False,
            ),
            sa.Column(
                "occurrences_at_last_post",
                sa.Integer(),
                server_default=sa.text("0"),
                nullable=False,
            ),
            sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_posted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("slack_thread_ts", sa.String(length=40), nullable=True),
            sa.Column("slack_message_ts", sa.String(length=40), nullable=True),
            sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("acknowledged_by", sa.String(length=120), nullable=True),
            sa.Column("acknowledgement", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("CURRENT_TIMESTAMP"),
                nullable=False,
            ),
            sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "org_id",
                "channel_id",
                "signature",
                name="uq_provider_alert_ledger_signature",
            ),
        )
    if not _index_exists(_TABLE, "ix_provider_alert_ledger_recent"):
        op.create_index(
            "ix_provider_alert_ledger_recent",
            _TABLE,
            ["org_id", "channel_id", "last_seen_at"],
        )


def downgrade() -> None:
    # Preserve acknowledgement history on downgrade. A later upgrade safely
    # recognizes the existing table and recreates only the missing index.
    if _index_exists(_TABLE, "ix_provider_alert_ledger_recent"):
        op.drop_index("ix_provider_alert_ledger_recent", table_name=_TABLE)
