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
_OLD_WORKSPACE_APPLIED = "ix_behavior_change_audits_workspace_applied"
_WORKSPACE_APPLIED = "ix_cycle_behavior_change_audits_workspace_applied"
_OLD_TARGET_HISTORY = "ix_behavior_change_audits_target_history"
_TARGET_HISTORY = "ix_cycle_behavior_change_audits_target_history"


def _has_table(table_name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(table_name)


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
        return

    op.execute(f'ALTER TABLE "{_OLD_TABLE}" RENAME TO "{_TABLE}"')
    op.execute(
        f'ALTER TABLE "{_TABLE}" RENAME CONSTRAINT '
        f'"{_OLD_TARGET_VERSION}" TO "{_TARGET_VERSION}"'
    )
    op.execute(
        f'ALTER TABLE "{_TABLE}" RENAME CONSTRAINT '
        f'"{_OLD_CYCLE_REVISION}" TO "{_CYCLE_REVISION}"'
    )
    op.execute(
        f'ALTER INDEX "{_OLD_WORKSPACE_APPLIED}" '
        f'RENAME TO "{_WORKSPACE_APPLIED}"'
    )
    op.execute(
        f'ALTER INDEX "{_OLD_TARGET_HISTORY}" '
        f'RENAME TO "{_TARGET_HISTORY}"'
    )

    # These two objects depended on the generic discriminator columns, so keep
    # their renamed identities while replacing their definitions.
    op.drop_index(_TARGET_HISTORY, table_name=_TABLE)
    op.drop_constraint(_TARGET_VERSION, _TABLE, type_="unique")
    op.execute(f'ALTER TABLE "{_TABLE}" DROP COLUMN "policy_kind"')
    op.execute(f'ALTER TABLE "{_TABLE}" DROP COLUMN "target_type"')
    op.create_unique_constraint(
        _TARGET_VERSION,
        _TABLE,
        ["target_id", "version"],
    )
    op.create_index(
        _TARGET_HISTORY,
        _TABLE,
        ["target_id", "version"],
    )


def downgrade() -> None:
    if _has_table(_OLD_TABLE) or not _has_table(_TABLE):
        return

    op.drop_index(_TARGET_HISTORY, table_name=_TABLE)
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
    op.execute(
        f'ALTER TABLE "{_TABLE}" RENAME CONSTRAINT '
        f'"{_CYCLE_REVISION}" TO "{_OLD_CYCLE_REVISION}"'
    )
    op.execute(
        f'ALTER INDEX "{_WORKSPACE_APPLIED}" '
        f'RENAME TO "{_OLD_WORKSPACE_APPLIED}"'
    )
    op.execute(f'ALTER TABLE "{_TABLE}" RENAME TO "{_OLD_TABLE}"')
