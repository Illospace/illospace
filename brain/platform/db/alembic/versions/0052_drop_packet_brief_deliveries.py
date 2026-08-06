"""Drop the retired packet brief delivery outbox.

Revision ID: 0052_drop_packet_brief_deliveries
Revises: 0051_open_ask_terminal_states
Create Date: 2026-08-06
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0052_drop_packet_brief_deliveries"
down_revision = "0051_open_ask_terminal_states"
branch_labels = None
depends_on = None

_TABLE = "packet_brief_deliveries"


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
    return (
        postgresql.UUID(as_uuid=False)
        if op.get_bind().dialect.name == "postgresql"
        else sa.String()
    )


def upgrade() -> None:
    if _table_exists(_TABLE):
        op.drop_table(_TABLE)


def downgrade() -> None:
    if not _table_exists(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("id", _uuid_type(), primary_key=True),
            sa.Column(
                "org_id",
                _uuid_type(),
                sa.ForeignKey("orgs.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "handoff_id",
                _uuid_type(),
                sa.ForeignKey("launch_handoffs.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "state",
                sa.String(length=20),
                nullable=False,
                server_default="pending",
            ),
            sa.Column("idempotency_key", sa.String(length=120), nullable=False),
            sa.Column("channel", sa.String(length=40), nullable=False),
            sa.Column("thread_ts", sa.String(length=40), nullable=False),
            sa.Column("bot_user_id", sa.String(length=40), nullable=True),
            sa.Column("brief", sa.Text(), nullable=False),
            sa.Column(
                "attempts",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("posted_message_ts", sa.String(length=40), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("CURRENT_TIMESTAMP"),
            ),
            sa.CheckConstraint(
                "state IN ('pending', 'posting', 'posted', 'superseded', 'failed')",
                name="ck_packet_brief_deliveries_state",
            ),
            sa.UniqueConstraint(
                "handoff_id",
                name="uq_packet_brief_deliveries_handoff",
            ),
        )

    if _table_exists(_TABLE) and not _index_exists(
        _TABLE,
        "ix_packet_brief_deliveries_org_state",
    ):
        op.create_index(
            "ix_packet_brief_deliveries_org_state",
            _TABLE,
            ["org_id", "state"],
        )
