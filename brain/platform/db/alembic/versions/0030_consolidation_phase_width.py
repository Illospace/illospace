"""Widen consolidation run phase names.

Revision ID: 0030_consolidation_phase_width
Revises: 0029_chantier_sunset_kind
Create Date: 2026-07-19
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0030_consolidation_phase_width"
down_revision = "0029_chantier_sunset_kind"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "consolidation_runs",
        "phase",
        existing_type=sa.String(length=20),
        type_=sa.String(length=64),
        existing_nullable=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    longest_phase = bind.execute(
        sa.text("SELECT MAX(length(phase)) FROM consolidation_runs")
    ).scalar_one_or_none()
    if longest_phase is not None and int(longest_phase) > 20:
        raise RuntimeError(
            "Cannot narrow consolidation_runs.phase to 20 characters while "
            "longer phase names are stored"
        )
    op.alter_column(
        "consolidation_runs",
        "phase",
        existing_type=sa.String(length=64),
        type_=sa.String(length=20),
        existing_nullable=False,
    )
