"""Add durable meetbot session records.

Revision ID: 0061_meetbot_sessions
Revises: 0060_storage_policies
Create Date: 2026-08-11
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0061_meetbot_sessions"
down_revision = "0060_storage_policies"
branch_labels = None
depends_on = None

_TABLE = "meetbot_sessions"


def upgrade() -> None:
    # The public baseline creates current model tables on fresh installs before
    # later migrations replay.
    if sa.inspect(op.get_bind()).has_table(_TABLE):
        return
    op.create_table(
        _TABLE,
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("meeting_url", sa.Text(), nullable=False),
        sa.Column("requesting_run_id", sa.Integer(), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "outcome",
            sa.String(length=20),
            server_default="requested",
            nullable=False,
        ),
        sa.Column("refusal_text", sa.Text(), nullable=True),
        sa.Column("admitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("left_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("participant_count", sa.Integer(), nullable=True),
        sa.Column("caption_count", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "outcome IN ('requested', 'admitted', 'refused', 'not_admitted', 'left')",
            name="ck_meetbot_sessions_outcome",
        ),
        sa.CheckConstraint(
            "participant_count IS NULL OR participant_count >= 0",
            name="ck_meetbot_sessions_participant_count_nonnegative",
        ),
        sa.CheckConstraint(
            "caption_count IS NULL OR caption_count >= 0",
            name="ck_meetbot_sessions_caption_count_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["requesting_run_id"],
            ["agent_runs.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("session_id"),
    )
    op.create_index(
        "ix_meetbot_sessions_meeting_requested",
        _TABLE,
        ["meeting_url", "requested_at"],
    )
    op.create_index(
        "ix_meetbot_sessions_requesting_run_id",
        _TABLE,
        ["requesting_run_id"],
    )


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table(_TABLE):
        op.drop_table(_TABLE)
