"""Persist scheduler cold-start gap reconciliation receipts.

Revision ID: 0050_scheduler_cold_start
Revises: 0049_agent_run_deadlines
Create Date: 2026-07-28
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0050_scheduler_cold_start"
down_revision = "0049_agent_run_deadlines"
branch_labels = None
depends_on = None

_RECEIPT_TABLE = "scheduler_cold_start_reconciliations"
_CHECKPOINT_TABLE = "scheduler_liveness_checkpoints"


def _schema() -> str | None:
    return "public" if op.get_bind().dialect.name == "postgresql" else None


def _table_exists(table: str) -> bool:
    return table in sa.inspect(op.get_bind()).get_table_names(schema=_schema())


def _json_type():
    if op.get_bind().dialect.name == "postgresql":
        return postgresql.JSONB(astext_type=sa.Text())
    return sa.JSON()


def upgrade() -> None:
    if not _table_exists(_RECEIPT_TABLE):
        op.create_table(
            _RECEIPT_TABLE,
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("gap_started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("reconciled_through", sa.DateTime(timezone=True), nullable=False),
            sa.Column(
                "status",
                sa.String(length=20),
                server_default=sa.text("'running'"),
                nullable=False,
            ),
            sa.Column(
                "lane_results",
                _json_type(),
                server_default=sa.text(
                    "'{}'::jsonb"
                    if op.get_bind().dialect.name == "postgresql"
                    else "'{}'"
                ),
                nullable=False,
            ),
            sa.Column(
                "notice_state",
                sa.String(length=20),
                server_default=sa.text("'pending'"),
                nullable=False,
            ),
            sa.Column("notice_marker", sa.String(length=80), nullable=False),
            sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column(
                "claim_generation",
                sa.Integer(),
                server_default=sa.text("1"),
                nullable=False,
            ),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("notice_posted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("notice_message_ts", sa.String(length=40), nullable=True),
            sa.Column("notice_client_msg_id", sa.String(length=36), nullable=False),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.CheckConstraint(
                "status IN ('pending', 'running', 'completed', 'degraded', 'failed')",
                name="ck_scheduler_cold_start_reconciliations_status",
            ),
            sa.CheckConstraint(
                "notice_state IN ('pending', 'posting', 'posted', 'failed')",
                name="ck_scheduler_cold_start_reconciliations_notice_state",
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "gap_started_at",
                "reconciled_through",
                name="uq_scheduler_cold_start_reconciliations_window",
            ),
            sa.UniqueConstraint(
                "notice_marker",
                name="uq_scheduler_cold_start_reconciliations_notice_marker",
            ),
            sa.UniqueConstraint(
                "notice_client_msg_id",
                name="uq_scheduler_cold_start_reconciliations_client_msg_id",
            ),
        )
    if not _table_exists(_CHECKPOINT_TABLE):
        op.create_table(
            _CHECKPOINT_TABLE,
            sa.Column("checkpoint_key", sa.String(length=80), nullable=False),
            sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_reconciled_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.text("now()"),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("checkpoint_key"),
        )


def downgrade() -> None:
    if _table_exists(_CHECKPOINT_TABLE):
        op.drop_table(_CHECKPOINT_TABLE)
    if _table_exists(_RECEIPT_TABLE):
        op.drop_table(_RECEIPT_TABLE)
