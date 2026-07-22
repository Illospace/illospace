"""Add durable provider-alert occurrence and surge state.

Revision ID: 0034_provider_alert_surges
Revises: 0033_provider_alert_ledger
Create Date: 2026-07-22
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0034_provider_alert_surges"
down_revision = "0033_provider_alert_ledger"
branch_labels = None
depends_on = None

_OCCURRENCES = "provider_alert_occurrences"
_SURGES = "provider_alert_surges"


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
    if not _table_exists(_OCCURRENCES):
        op.create_table(
            _OCCURRENCES,
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("org_id", _uuid_type(), nullable=False),
            sa.Column("channel_id", sa.String(length=80), nullable=False),
            sa.Column("slack_message_ts", sa.String(length=40), nullable=False),
            sa.Column("service", sa.String(length=120), nullable=False),
            sa.Column("subsystem", sa.String(length=120), nullable=False),
            sa.Column("external_id", sa.String(length=180), nullable=False),
            sa.Column("signature", sa.String(length=64), nullable=False),
            sa.Column("signature_title", sa.Text(), nullable=False),
            sa.Column("occurrence_milestone", sa.Integer(), nullable=True),
            sa.Column(
                "is_new_error",
                sa.Boolean(),
                server_default=sa.text("false"),
                nullable=False,
            ),
            sa.Column(
                "is_new_signature",
                sa.Boolean(),
                server_default=sa.text("false"),
                nullable=False,
            ),
            sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
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
                "slack_message_ts",
                name="uq_provider_alert_occurrence_message",
            ),
        )
    if not _index_exists(_OCCURRENCES, "ix_provider_alert_occurrence_window"):
        op.create_index(
            "ix_provider_alert_occurrence_window",
            _OCCURRENCES,
            ["org_id", "channel_id", "service", "occurred_at"],
        )
    if not _index_exists(_OCCURRENCES, "ix_provider_alert_occurrence_signature"):
        op.create_index(
            "ix_provider_alert_occurrence_signature",
            _OCCURRENCES,
            ["org_id", "service", "subsystem", "signature"],
        )

    if not _table_exists(_SURGES):
        op.create_table(
            _SURGES,
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("org_id", _uuid_type(), nullable=False),
            sa.Column("source_channel_id", sa.String(length=80), nullable=False),
            sa.Column("service", sa.String(length=120), nullable=False),
            sa.Column("subsystem", sa.String(length=120), nullable=False),
            sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("trigger_reason", sa.String(length=80), nullable=False),
            sa.Column("message_count", sa.Integer(), nullable=False),
            sa.Column("signatures_json", sa.Text(), nullable=False),
            sa.Column("external_ids_json", sa.Text(), nullable=False),
            sa.Column("owner", sa.String(length=180), nullable=False),
            sa.Column("next_action", sa.Text(), nullable=False),
            sa.Column("material_channel", sa.String(length=80), nullable=False),
            sa.Column("material_message_ts", sa.String(length=40), nullable=True),
            sa.Column("material_post_claimed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("material_posted_at", sa.DateTime(timezone=True), nullable=True),
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
                "source_channel_id",
                "service",
                name="uq_provider_alert_surge_service",
            ),
        )
    if not _index_exists(_SURGES, "ix_provider_alert_surge_open"):
        op.create_index(
            "ix_provider_alert_surge_open",
            _SURGES,
            ["org_id", "last_seen_at"],
        )


def downgrade() -> None:
    # Preserve alert/incident history. A later upgrade recognizes both tables
    # and recreates only missing indexes, matching the durable 0033 ledger.
    if _index_exists(_SURGES, "ix_provider_alert_surge_open"):
        op.drop_index("ix_provider_alert_surge_open", table_name=_SURGES)
    if _index_exists(_OCCURRENCES, "ix_provider_alert_occurrence_signature"):
        op.drop_index(
            "ix_provider_alert_occurrence_signature",
            table_name=_OCCURRENCES,
        )
    if _index_exists(_OCCURRENCES, "ix_provider_alert_occurrence_window"):
        op.drop_index("ix_provider_alert_occurrence_window", table_name=_OCCURRENCES)
