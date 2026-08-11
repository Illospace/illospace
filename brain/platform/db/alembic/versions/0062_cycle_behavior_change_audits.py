"""Make the behavior-change audit envelope explicitly Cycle-only.

Revision ID: 0062_cycle_behavior_change_audits
Revises: 0061_meetbot_sessions
Create Date: 2026-08-11
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0062_cycle_behavior_change_audits"
down_revision = "0061_meetbot_sessions"
branch_labels = None
depends_on = None

_OLD_TABLE = "behavior_change_audits"
_TABLE = "cycle_behavior_change_audits"

_OLD_TARGET_VERSION = "uq_behavior_change_audits_target_version"
_TARGET_VERSION = "uq_cycle_behavior_change_audits_target_version"
_OLD_CYCLE_REVISION = "uq_behavior_change_audits_cycle_revision"
_CYCLE_REVISION = "uq_cycle_behavior_change_audits_cycle_revision"
_OLD_PRIMARY_KEY = "behavior_change_audits_pkey"
_PRIMARY_KEY = "cycle_behavior_change_audits_pkey"
_OLD_CYCLE_REVISION_FK = "behavior_change_audits_cycle_revision_id_fkey"
_CYCLE_REVISION_FK = "cycle_behavior_change_audits_cycle_revision_id_fkey"
_OLD_REVERTED_FROM_FK = "behavior_change_audits_reverted_from_id_fkey"
_REVERTED_FROM_FK = "cycle_behavior_change_audits_reverted_from_id_fkey"
_OLD_WORKSPACE_APPLIED = "ix_behavior_change_audits_workspace_applied"
_WORKSPACE_APPLIED = "ix_cycle_behavior_change_audits_workspace_applied"
_OLD_TARGET_HISTORY = "ix_behavior_change_audits_target_history"

_CONSTRAINT_RENAMES = (
    (_OLD_PRIMARY_KEY, _PRIMARY_KEY),
    (_OLD_CYCLE_REVISION_FK, _CYCLE_REVISION_FK),
    (_OLD_REVERTED_FROM_FK, _REVERTED_FROM_FK),
    (_OLD_CYCLE_REVISION, _CYCLE_REVISION),
)
_INDEX_RENAMES = ((_OLD_WORKSPACE_APPLIED, _WORKSPACE_APPLIED),)


def _has_table(table_name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table_name)


def _has_constraint(table_name: str, constraint_name: str) -> bool:
    return bool(
        op.get_bind()
        .execute(
            sa.text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_constraint
                    WHERE conrelid = to_regclass(:table_name)
                      AND conname = :constraint_name
                )
                """
            ),
            {
                "table_name": table_name,
                "constraint_name": constraint_name,
            },
        )
        .scalar_one()
    )


def _has_index(table_name: str, index_name: str) -> bool:
    return bool(
        op.get_bind()
        .execute(
            sa.text(
                """
                SELECT EXISTS (
                    SELECT 1
                    FROM pg_class AS index_relation
                    JOIN pg_index
                      ON pg_index.indexrelid = index_relation.oid
                    WHERE pg_index.indrelid = to_regclass(:table_name)
                      AND index_relation.relname = :index_name
                )
                """
            ),
            {
                "table_name": table_name,
                "index_name": index_name,
            },
        )
        .scalar_one()
    )


def _rename_constraint_if_exists(
    table_name: str,
    old_name: str,
    new_name: str,
) -> None:
    if _has_constraint(table_name, old_name):
        op.execute(
            f'ALTER TABLE "{table_name}" RENAME CONSTRAINT '
            f'"{old_name}" TO "{new_name}"'
        )


def _rename_index_if_exists(
    table_name: str,
    old_name: str,
    new_name: str,
) -> None:
    if _has_index(table_name, old_name):
        op.execute(f'ALTER INDEX "{old_name}" RENAME TO "{new_name}"')


def upgrade() -> None:
    old_exists = _has_table(_OLD_TABLE)
    new_exists = _has_table(_TABLE)

    if new_exists:
        if old_exists:
            old_row_count = op.get_bind().execute(
                sa.text(f'SELECT COUNT(*) FROM "{_OLD_TABLE}"')
            ).scalar_one()
            if old_row_count:
                raise RuntimeError(
                    "both behavior-change audit tables contain schema state; "
                    "refusing to discard the old table"
                )
            # Fresh installs materialize current model metadata in the public
            # baseline, then 0059 creates this empty historical table.
            op.drop_table(_OLD_TABLE)
        return

    if not old_exists:
        raise RuntimeError(
            "neither behavior-change audit table exists; refusing to mark "
            "the migration as applied"
        )

    op.execute(f'ALTER TABLE "{_OLD_TABLE}" RENAME TO "{_TABLE}"')
    for old_name, new_name in _CONSTRAINT_RENAMES:
        _rename_constraint_if_exists(_TABLE, old_name, new_name)
    for old_name, new_name in _INDEX_RENAMES:
        _rename_index_if_exists(_TABLE, old_name, new_name)

    # The target-version objects duplicate each other. Keep only the unique
    # constraint, replacing it because the generic discriminator columns go.
    if _has_index(_TABLE, _OLD_TARGET_HISTORY):
        op.drop_index(_OLD_TARGET_HISTORY, table_name=_TABLE)
    op.drop_constraint(_OLD_TARGET_VERSION, _TABLE, type_="unique")
    op.execute(f'ALTER TABLE "{_TABLE}" DROP COLUMN "policy_kind"')
    op.execute(f'ALTER TABLE "{_TABLE}" DROP COLUMN "target_type"')
    op.create_unique_constraint(
        _TARGET_VERSION,
        _TABLE,
        ["target_id", "version"],
    )


def downgrade() -> None:
    if _has_table(_OLD_TABLE) or not _has_table(_TABLE):
        return

    op.drop_constraint(_TARGET_VERSION, _TABLE, type_="unique")
    op.add_column(
        _TABLE,
        sa.Column(
            "policy_kind",
            sa.String(length=50),
            server_default=sa.text("'cycle'"),
            nullable=False,
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "target_type",
            sa.String(length=50),
            server_default=sa.text("'cycle'"),
            nullable=False,
        ),
    )
    op.alter_column(
        _TABLE,
        "policy_kind",
        existing_type=sa.String(length=50),
        existing_nullable=False,
        server_default=None,
    )
    op.alter_column(
        _TABLE,
        "target_type",
        existing_type=sa.String(length=50),
        existing_nullable=False,
        server_default=None,
    )
    op.create_unique_constraint(
        _OLD_TARGET_VERSION,
        _TABLE,
        ["policy_kind", "target_type", "target_id", "version"],
    )
    op.create_index(
        _OLD_TARGET_HISTORY,
        _TABLE,
        ["policy_kind", "target_type", "target_id", "version"],
    )
    for old_name, new_name in _CONSTRAINT_RENAMES:
        _rename_constraint_if_exists(_TABLE, new_name, old_name)
    for old_name, new_name in _INDEX_RENAMES:
        _rename_index_if_exists(_TABLE, new_name, old_name)
    op.execute(f'ALTER TABLE "{_TABLE}" RENAME TO "{_OLD_TABLE}"')
