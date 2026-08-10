"""Delete orphaned standing-failure trigger-state rows.

Revision ID: 0058_delete_orphaned_standing_failure_states
Revises: 0057_retrieval_log_timestamptz
Create Date: 2026-08-10
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0058_delete_orphaned_standing_failure_states"
down_revision = "0057_retrieval_log_timestamptz"
branch_labels = None
depends_on = None

_TABLE = "scheduler_failure_guard_trigger_states"


def upgrade() -> None:
    # No explicit schema: the presence check must resolve through the same
    # search_path the DELETE below uses, or it silently skips on a non-public
    # schema.
    if sa.inspect(op.get_bind()).has_table(_TABLE):
        op.execute(
            sa.text(
                "DELETE FROM scheduler_failure_guard_trigger_states "
                "WHERE trigger_kind = 'standing_failure'"
            )
        )


def downgrade() -> None:
    # The rows were a drifting cache with wrong values, so nothing can be restored.
    pass
