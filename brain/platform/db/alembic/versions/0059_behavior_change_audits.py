"""Add the immutable behavior-change audit envelope.

Revision ID: 0059_behavior_change_audits
Revises: 0058_delete_orphaned_standing_failure_states
Create Date: 2026-08-10
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0059_behavior_change_audits"
down_revision = "0058_delete_orphaned_standing_failure_states"
branch_labels = None
depends_on = None

_TABLE = "behavior_change_audits"


def upgrade() -> None:
    # The public baseline creates current model tables on fresh installs before
    # later migrations replay.
    if sa.inspect(op.get_bind()).has_table(_TABLE):
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("workspace_id", sa.Text(), nullable=False),
        sa.Column("policy_kind", sa.String(length=50), nullable=False),
        sa.Column("target_type", sa.String(length=50), nullable=False),
        sa.Column("target_id", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("actor_type", sa.String(length=30), nullable=False),
        sa.Column("actor_id", sa.Text(), nullable=False),
        sa.Column("source_reference", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("before_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("after_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("changed_fields", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("cycle_revision_id", sa.Integer(), nullable=False),
        sa.Column("reverted_from_id", sa.Integer(), nullable=True),
        sa.Column(
            "applied_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["cycle_revision_id"],
            ["cycle_revisions.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["reverted_from_id"],
            ["behavior_change_audits.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "cycle_revision_id",
            name="uq_behavior_change_audits_cycle_revision",
        ),
        sa.UniqueConstraint(
            "policy_kind",
            "target_type",
            "target_id",
            "version",
            name="uq_behavior_change_audits_target_version",
        ),
    )
    op.create_index(
        "ix_behavior_change_audits_workspace_applied",
        "behavior_change_audits",
        ["workspace_id", "applied_at", "id"],
    )
    op.create_index(
        "ix_behavior_change_audits_target_history",
        "behavior_change_audits",
        ["policy_kind", "target_type", "target_id", "version"],
    )


def downgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table(_TABLE):
        return
    op.drop_index(
        "ix_behavior_change_audits_target_history",
        table_name=_TABLE,
    )
    op.drop_index(
        "ix_behavior_change_audits_workspace_applied",
        table_name=_TABLE,
    )
    op.drop_table(_TABLE)
