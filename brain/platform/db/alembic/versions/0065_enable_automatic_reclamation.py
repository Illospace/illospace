"""Enable automatic workspace reclamation for the untouched seed policy."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0065_enable_automatic_reclamation"
down_revision = "0064_cycle_receipt_monitoring"
branch_labels = None
depends_on = None

_SEED_RATIONALE = "Initial policy migrated from deployed retention behavior."
_ENABLED_RATIONALE = "Illospace issue #876 enabled automatic reclamation."


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE storage_policies
            SET automatic_reclamation_allowed = TRUE,
                rationale = :enabled_rationale
            WHERE is_active = TRUE
              AND rationale = :seed_rationale
            """
        ).bindparams(
            enabled_rationale=_ENABLED_RATIONALE,
            seed_rationale=_SEED_RATIONALE,
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE storage_policies
            SET automatic_reclamation_allowed = FALSE,
                rationale = :seed_rationale
            WHERE is_active = TRUE
              AND automatic_reclamation_allowed = TRUE
              AND rationale = :enabled_rationale
            """
        ).bindparams(
            enabled_rationale=_ENABLED_RATIONALE,
            seed_rationale=_SEED_RATIONALE,
        )
    )
