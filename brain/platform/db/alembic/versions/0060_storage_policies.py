"""Add runtime-editable storage policy revisions.

Revision ID: 0060_storage_policies
Revises: 0059_behavior_change_audits
Create Date: 2026-08-10
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0060_storage_policies"
down_revision = "0059_behavior_change_audits"
branch_labels = None
depends_on = None

_TABLE = "storage_policies"


def _table_exists() -> bool:
    return sa.inspect(op.get_bind()).has_table(_TABLE)


def _index_exists(index_name: str) -> bool:
    if not _table_exists():
        return False
    return any(
        index.get("name") == index_name
        for index in sa.inspect(op.get_bind()).get_indexes(_TABLE)
    )


def upgrade() -> None:
    # The public baseline creates current model tables on fresh installs before
    # later migrations replay.
    if not _table_exists():
        op.create_table(
            _TABLE,
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column(
                "finished_workspace_retention_hours",
                sa.Integer(),
                nullable=False,
            ),
            sa.Column("project_draft_retention_hours", sa.Integer(), nullable=False),
            sa.Column("canvas_quiet_hours", sa.Integer(), nullable=False),
            sa.Column("capacity_warn_percent", sa.Integer(), nullable=False),
            sa.Column("capacity_critical_percent", sa.Integer(), nullable=False),
            sa.Column(
                "automatic_reclamation_allowed",
                sa.Boolean(),
                nullable=False,
            ),
            sa.Column("rationale", sa.Text(), nullable=False),
            sa.Column(
                "source_type",
                sa.String(length=30),
                server_default="system",
                nullable=False,
            ),
            sa.Column("source_id", sa.Text(), nullable=True),
            sa.Column("reverted_from_id", sa.Integer(), nullable=True),
            sa.Column(
                "is_active",
                sa.Boolean(),
                server_default=sa.text("TRUE"),
                nullable=False,
            ),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.CheckConstraint(
                "finished_workspace_retention_hours > 0",
                name="ck_storage_policies_finished_workspace_retention_positive",
            ),
            sa.CheckConstraint(
                "project_draft_retention_hours > 0",
                name="ck_storage_policies_project_draft_retention_positive",
            ),
            sa.CheckConstraint(
                "canvas_quiet_hours > 0",
                name="ck_storage_policies_canvas_quiet_positive",
            ),
            sa.CheckConstraint(
                "capacity_warn_percent >= 1 AND capacity_warn_percent <= 99",
                name="ck_storage_policies_warn_percent",
            ),
            sa.CheckConstraint(
                "capacity_critical_percent >= 2 AND capacity_critical_percent <= 100",
                name="ck_storage_policies_critical_percent",
            ),
            sa.CheckConstraint(
                "capacity_warn_percent < capacity_critical_percent",
                name="ck_storage_policies_threshold_order",
            ),
            sa.ForeignKeyConstraint(
                ["reverted_from_id"],
                ["storage_policies.id"],
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id"),
        )

    if not _index_exists("uq_storage_policies_one_active"):
        op.create_index(
            "uq_storage_policies_one_active",
            _TABLE,
            ["is_active"],
            unique=True,
            postgresql_where=sa.text("is_active"),
        )
    if not _index_exists("ix_storage_policies_created"):
        op.create_index(
            "ix_storage_policies_created",
            _TABLE,
            ["created_at", "id"],
        )

    active_count = op.get_bind().execute(
        sa.text("SELECT count(*) FROM storage_policies WHERE is_active = TRUE")
    ).scalar_one()
    # This guarded seed only runs for fresh databases. Changing its defaults
    # does not mutate deployments where this migration has already run.
    if active_count == 0:
        op.execute(
            sa.text(
                """
                INSERT INTO storage_policies (
                    finished_workspace_retention_hours,
                    project_draft_retention_hours,
                    canvas_quiet_hours,
                    capacity_warn_percent,
                    capacity_critical_percent,
                    automatic_reclamation_allowed,
                    rationale,
                    source_type,
                    is_active
                ) VALUES (
                    48,
                    168,
                    24,
                    80,
                    90,
                    TRUE,
                    'Automatic workspace reclamation is enabled by default.',
                    'system',
                    TRUE
                )
                """
            )
        )


def downgrade() -> None:
    if _table_exists():
        op.drop_table(_TABLE)
