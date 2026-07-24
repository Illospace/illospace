"""Move the fleet default back to GPT-5.5 on latency grounds.

GPT-5.6 Sol is a preview model and is 2-4x slower per call than GPT-5.5 at
every effort tier (measured on illo-dev 2026-07-24: 5.5 ~18-20s; 5.6-sol
43s at low, 72s at medium, 79s at high). Runs average 20-40 turns, so the
fleet default at 5.6-sol pushed run duration past 40 minutes, saturated the
worker, and dropped completions to zero while the queue grew.

Effort routing cannot recover this: 5.6-sol at `low` is still slower than 5.5
at `high`. The routing layer itself is unchanged and correct — only the model
the fleet defaults to moves back until 5.6-sol is fast enough to carry it.

Revision ID: 0040_default_model_latency_rollback
Revises: 0039_clear_revision_model_pins
Create Date: 2026-07-24
"""

from __future__ import annotations

from alembic import op


revision = "0040_default_model_latency_rollback"
down_revision = "0039_clear_revision_model_pins"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE orgs
        SET memory_model_config = COALESCE(memory_model_config, '{}'::jsonb)
            || jsonb_build_object('default_model', 'openai/gpt-5.5')
        WHERE memory_model_config->>'default_model' = 'openai/gpt-5.6-sol'
        """
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE orgs
        SET memory_model_config = COALESCE(memory_model_config, '{}'::jsonb)
            || jsonb_build_object('default_model', 'openai/gpt-5.6-sol')
        WHERE memory_model_config->>'default_model' = 'openai/gpt-5.5'
        """
    )
