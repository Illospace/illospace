"""Add durable Cycle memory primitives.

Revision ID: 0013_cycle_memory_primitives
Revises: 0012_app_capsule_workspace_app_defaults
Create Date: 2026-05-27
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0013_cycle_memory_primitives"
down_revision = "0012_app_capsule_workspace_app_defaults"
branch_labels = None
depends_on = None


def _schema() -> str | None:
    return "public" if op.get_bind().dialect.name == "postgresql" else None


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _table_exists(table_name: str) -> bool:
    return table_name in _inspector().get_table_names(schema=_schema())


def _column_exists(table_name: str, column_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return any(
        column.get("name") == column_name
        for column in _inspector().get_columns(table_name, schema=_schema())
    )


def _index_exists(table_name: str, index_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return any(
        index.get("name") == index_name
        for index in _inspector().get_indexes(table_name, schema=_schema())
    )


def _foreign_key_exists(table_name: str, constraint_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return any(
        foreign_key.get("name") == constraint_name
        for foreign_key in _inspector().get_foreign_keys(table_name, schema=_schema())
    )


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if _table_exists(table_name) and not _column_exists(table_name, column.name):
        op.add_column(table_name, column)


def _drop_column_if_exists(table_name: str, column_name: str) -> None:
    if _table_exists(table_name) and _column_exists(table_name, column_name):
        op.drop_column(table_name, column_name)


def upgrade() -> None:
    _add_column_if_missing(
        "cycles",
        sa.Column("creator_type", sa.String(length=30), nullable=False, server_default="user"),
    )
    _add_column_if_missing("cycles", sa.Column("creator_id", sa.Text(), nullable=True))
    _add_column_if_missing(
        "cycles",
        sa.Column("maintainer_type", sa.String(length=30), nullable=False, server_default="user"),
    )
    _add_column_if_missing("cycles", sa.Column("maintainer_id", sa.Text(), nullable=True))

    if _table_exists("cycles"):
        op.execute(
            sa.text(
                """
                UPDATE cycles
                SET creator_id = COALESCE(creator_id, user_id::text),
                    maintainer_id = COALESCE(maintainer_id, user_id::text)
                """
            )
        )

    if not _table_exists("cycle_revisions"):
        op.create_table(
            "cycle_revisions",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("cycle_id", sa.Integer(), nullable=False),
            sa.Column("revision_number", sa.Integer(), nullable=False),
            sa.Column("source_type", sa.String(length=30), nullable=False, server_default="system"),
            sa.Column("source_id", sa.Text(), nullable=True),
            sa.Column("rationale", sa.Text(), nullable=True),
            sa.Column("name", sa.Text(), nullable=False),
            sa.Column("prompt", sa.Text(), nullable=False),
            sa.Column("schedule_expr", sa.Text(), nullable=False),
            sa.Column("timezone", sa.String(length=64), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("model_override", sa.Text(), nullable=True),
            sa.Column("thinking_override", sa.String(length=20), nullable=True),
            sa.Column("target_idea_id", postgresql.UUID(as_uuid=False), nullable=True),
            sa.Column(
                "context_policy",
                postgresql.JSONB(),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["cycle_id"], ["cycles.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("cycle_id", "revision_number", name="uq_cycle_revisions_cycle_number"),
        )
        op.create_index("ix_cycle_revisions_cycle_id", "cycle_revisions", ["cycle_id"])
        op.create_index(
            "ix_cycle_revisions_cycle_created",
            "cycle_revisions",
            ["cycle_id", "created_at", "id"],
        )

    if not _table_exists("cycle_guidance"):
        op.create_table(
            "cycle_guidance",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("cycle_id", sa.Integer(), nullable=False),
            sa.Column("revision_id", sa.Integer(), nullable=True),
            sa.Column("source_type", sa.String(length=30), nullable=False, server_default="user"),
            sa.Column("source_id", sa.Text(), nullable=True),
            sa.Column("guidance", sa.Text(), nullable=False),
            sa.Column("rationale", sa.Text(), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("TRUE")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["cycle_id"], ["cycles.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["revision_id"], ["cycle_revisions.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_cycle_guidance_cycle_id", "cycle_guidance", ["cycle_id"])
        op.create_index("ix_cycle_guidance_revision_id", "cycle_guidance", ["revision_id"])
        op.create_index(
            "ix_cycle_guidance_cycle_active",
            "cycle_guidance",
            ["cycle_id", "is_active", "created_at"],
        )

    if not _table_exists("cycle_output_targets"):
        op.create_table(
            "cycle_output_targets",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("cycle_id", sa.Integer(), nullable=False),
            sa.Column("revision_id", sa.Integer(), nullable=True),
            sa.Column("target_type", sa.String(length=50), nullable=False),
            sa.Column("target_id", sa.Text(), nullable=True),
            sa.Column("label", sa.Text(), nullable=True),
            sa.Column(
                "config",
                postgresql.JSONB(),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
            sa.Column("source_type", sa.String(length=30), nullable=False, server_default="user"),
            sa.Column("source_id", sa.Text(), nullable=True),
            sa.Column("rationale", sa.Text(), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("TRUE")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["cycle_id"], ["cycles.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["revision_id"], ["cycle_revisions.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_cycle_output_targets_cycle_id", "cycle_output_targets", ["cycle_id"])
        op.create_index("ix_cycle_output_targets_revision_id", "cycle_output_targets", ["revision_id"])
        op.create_index(
            "ix_cycle_output_targets_cycle_active",
            "cycle_output_targets",
            ["cycle_id", "is_active", "target_type"],
        )

    _add_column_if_missing(
        "cycle_runs",
        sa.Column("revision_id", sa.Integer(), nullable=True),
    )
    _add_column_if_missing(
        "cycle_runs",
        sa.Column(
            "guidance_snapshot",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    _add_column_if_missing(
        "cycle_runs",
        sa.Column(
            "output_targets_snapshot",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    _add_column_if_missing(
        "cycle_runs",
        sa.Column(
            "context_snapshot",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    _add_column_if_missing("cycle_runs", sa.Column("self_review_summary", sa.Text(), nullable=True))

    if _table_exists("cycle_runs") and _table_exists("cycle_revisions"):
        if not _foreign_key_exists("cycle_runs", "fk_cycle_runs_revision_id_cycle_revisions"):
            op.create_foreign_key(
                "fk_cycle_runs_revision_id_cycle_revisions",
                "cycle_runs",
                "cycle_revisions",
                ["revision_id"],
                ["id"],
                ondelete="SET NULL",
            )
        if not _index_exists("cycle_runs", "ix_cycle_runs_revision_id"):
            op.create_index("ix_cycle_runs_revision_id", "cycle_runs", ["revision_id"])

    if not _table_exists("cycle_run_evaluations"):
        op.create_table(
            "cycle_run_evaluations",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("cycle_id", sa.Integer(), nullable=False),
            sa.Column("cycle_run_id", sa.Integer(), nullable=False),
            sa.Column("evaluator_type", sa.String(length=30), nullable=False, server_default="system"),
            sa.Column("evaluator_id", sa.Text(), nullable=True),
            sa.Column("summary", sa.Text(), nullable=False),
            sa.Column("score", sa.Integer(), nullable=True),
            sa.Column(
                "details",
                postgresql.JSONB(),
                nullable=False,
                server_default=sa.text("'{}'::jsonb"),
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
            sa.ForeignKeyConstraint(["cycle_id"], ["cycles.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["cycle_run_id"], ["cycle_runs.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_cycle_run_evaluations_cycle_id", "cycle_run_evaluations", ["cycle_id"])
        op.create_index("ix_cycle_run_evaluations_cycle_run_id", "cycle_run_evaluations", ["cycle_run_id"])
        op.create_index(
            "ix_cycle_run_evaluations_cycle_created",
            "cycle_run_evaluations",
            ["cycle_id", "created_at", "id"],
        )
        op.create_index(
            "ix_cycle_run_evaluations_run_created",
            "cycle_run_evaluations",
            ["cycle_run_id", "created_at", "id"],
        )

    if _table_exists("cycles") and _table_exists("cycle_revisions"):
        op.execute(
            sa.text(
                """
                INSERT INTO cycle_revisions (
                    cycle_id,
                    revision_number,
                    source_type,
                    source_id,
                    rationale,
                    name,
                    prompt,
                    schedule_expr,
                    timezone,
                    enabled,
                    model_override,
                    thinking_override,
                    target_idea_id,
                    context_policy,
                    created_at
                )
                SELECT
                    cycles.id,
                    1,
                    'system',
                    NULL,
                    'Backfilled from existing Cycle row.',
                    cycles.name,
                    cycles.prompt,
                    cycles.schedule_expr,
                    cycles.timezone,
                    cycles.enabled,
                    cycles.model_override,
                    cycles.thinking_override,
                    cycles.target_idea_id,
                    '{}'::jsonb,
                    COALESCE(cycles.created_at, NOW())
                FROM cycles
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM cycle_revisions revisions
                    WHERE revisions.cycle_id = cycles.id
                )
                """
            )
        )

    if _table_exists("cycles") and _table_exists("cycle_output_targets"):
        op.execute(
            sa.text(
                """
                INSERT INTO cycle_output_targets (
                    cycle_id,
                    target_type,
                    target_id,
                    label,
                    config,
                    source_type,
                    source_id,
                    rationale,
                    is_active,
                    created_at,
                    updated_at
                )
                SELECT
                    cycles.id,
                    'cycle_ledger',
                    cycles.id::text,
                    'Cycle ledger',
                    '{}'::jsonb,
                    'system',
                    NULL,
                    'Default durable Cycle output target.',
                    TRUE,
                    NOW(),
                    NOW()
                FROM cycles
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM cycle_output_targets targets
                    WHERE targets.cycle_id = cycles.id
                      AND targets.target_type = 'cycle_ledger'
                      AND targets.target_id = cycles.id::text
                )
                """
            )
        )
        op.execute(
            sa.text(
                """
                INSERT INTO cycle_output_targets (
                    cycle_id,
                    target_type,
                    target_id,
                    label,
                    config,
                    source_type,
                    source_id,
                    rationale,
                    is_active,
                    created_at,
                    updated_at
                )
                SELECT
                    cycles.id,
                    'thread',
                    cycles.target_idea_id::text,
                    'Cycle thread',
                    '{}'::jsonb,
                    'system',
                    NULL,
                    'Backfilled from Cycle target thread.',
                    TRUE,
                    NOW(),
                    NOW()
                FROM cycles
                WHERE cycles.target_idea_id IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1
                      FROM cycle_output_targets targets
                      WHERE targets.cycle_id = cycles.id
                        AND targets.target_type = 'thread'
                        AND targets.target_id = cycles.target_idea_id::text
                  )
                """
            )
        )


def downgrade() -> None:
    if _table_exists("cycle_run_evaluations"):
        op.drop_table("cycle_run_evaluations")

    if _table_exists("cycle_runs"):
        if _index_exists("cycle_runs", "ix_cycle_runs_revision_id"):
            op.drop_index("ix_cycle_runs_revision_id", table_name="cycle_runs")
        if _foreign_key_exists("cycle_runs", "fk_cycle_runs_revision_id_cycle_revisions"):
            op.drop_constraint("fk_cycle_runs_revision_id_cycle_revisions", "cycle_runs", type_="foreignkey")
    _drop_column_if_exists("cycle_runs", "self_review_summary")
    _drop_column_if_exists("cycle_runs", "context_snapshot")
    _drop_column_if_exists("cycle_runs", "output_targets_snapshot")
    _drop_column_if_exists("cycle_runs", "guidance_snapshot")
    _drop_column_if_exists("cycle_runs", "revision_id")

    if _table_exists("cycle_output_targets"):
        op.drop_table("cycle_output_targets")
    if _table_exists("cycle_guidance"):
        op.drop_table("cycle_guidance")
    if _table_exists("cycle_revisions"):
        op.drop_table("cycle_revisions")

    _drop_column_if_exists("cycles", "maintainer_id")
    _drop_column_if_exists("cycles", "maintainer_type")
    _drop_column_if_exists("cycles", "creator_id")
    _drop_column_if_exists("cycles", "creator_type")
