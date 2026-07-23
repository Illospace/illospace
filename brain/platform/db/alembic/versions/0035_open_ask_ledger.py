"""Add the durable open-ask ledger.

Revision ID: 0035_open_ask_ledger
Revises: 0034_provider_alert_surges
Create Date: 2026-07-23
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0035_open_ask_ledger"
down_revision = "0034_provider_alert_surges"
branch_labels = None
depends_on = None

_TABLE = "open_asks"


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
            sa.Column("channel_type", sa.String(length=32), nullable=True),
            sa.Column("team_id", sa.String(length=80), nullable=True),
            sa.Column("thread_ts", sa.String(length=40), nullable=False),
            sa.Column("thread_permalink", sa.Text(), nullable=False),
            sa.Column("requester_slack_id", sa.String(length=80), nullable=False),
            sa.Column("requester_user_id", _uuid_type(), nullable=True),
            sa.Column("requester_name", sa.String(length=120), nullable=True),
            sa.Column("bot_user_id", sa.String(length=80), nullable=True),
            sa.Column("ask_text", sa.Text(), nullable=False),
            sa.Column("origin_ref", sa.String(length=500), nullable=False),
            sa.Column("origin_run_id", sa.Integer(), nullable=True),
            sa.Column(
                "status",
                sa.String(length=20),
                server_default=sa.text("'open'"),
                nullable=False,
            ),
            sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("answer_text", sa.Text(), nullable=True),
            sa.Column("answer_artifact_kind", sa.String(length=80), nullable=True),
            sa.Column("answer_artifact_ref", sa.Text(), nullable=True),
            sa.Column("answered_by_run_id", sa.Integer(), nullable=True),
            sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("delivered_message_ts", sa.String(length=40), nullable=True),
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
            sa.CheckConstraint(
                "status IN ('open', 'answered')",
                name="ck_open_asks_status",
            ),
            sa.ForeignKeyConstraint(["org_id"], ["orgs.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(
                ["requester_user_id"],
                ["users.id"],
                ondelete="SET NULL",
            ),
            sa.ForeignKeyConstraint(
                ["origin_run_id"],
                ["agent_runs.id"],
                ondelete="SET NULL",
            ),
            sa.ForeignKeyConstraint(
                ["answered_by_run_id"],
                ["agent_runs.id"],
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "org_id",
                "channel_id",
                "thread_ts",
                "requester_slack_id",
                name="uq_open_asks_slack_requester",
            ),
        )
    for index_name, columns in (
        (
            "ix_open_asks_org_status_opened",
            ["org_id", "status", "opened_at"],
        ),
        ("ix_open_asks_origin_ref", ["org_id", "origin_ref"]),
        ("ix_open_asks_origin_run", ["origin_run_id"]),
    ):
        if not _index_exists(_TABLE, index_name):
            op.create_index(index_name, _TABLE, columns)


def downgrade() -> None:
    # Preserve unanswered human obligations and their delivery history.
    return None
