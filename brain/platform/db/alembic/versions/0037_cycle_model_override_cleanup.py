"""Clear legacy default Cycle model overrides.

Revision ID: 0037_cycle_model_override_cleanup
Revises: 0036_chantier_members_blockers, 0036_exception_ping_state
Create Date: 2026-07-24
"""

from __future__ import annotations

from alembic import op


revision = "0037_cycle_model_override_cleanup"
down_revision = (
    "0036_chantier_members_blockers",
    "0036_exception_ping_state",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE cycles "
        "SET model_override = NULL "
        "WHERE model_override IN ('', 'default')"
    )


def downgrade() -> None:
    return None
