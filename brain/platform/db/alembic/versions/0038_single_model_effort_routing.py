"""Route one model with per-cycle effort only.

Illo runs a single strong model and varies reasoning effort. Cycles stop
pinning their own models (`thinking_override` keeps steering effort), and the
org default moves to GPT-5.6 Sol with an explicit default effort tier.

Revision ID: 0038_single_model_effort_routing
Revises: 0037_cycle_model_override_cleanup
Create Date: 2026-07-24
"""

from __future__ import annotations

from alembic import op


revision = "0038_single_model_effort_routing"
down_revision = "0037_cycle_model_override_cleanup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE cycles SET model_override = NULL WHERE model_override IS NOT NULL")
    op.execute(
        """
        UPDATE orgs
        SET memory_model_config = COALESCE(memory_model_config, '{}'::jsonb)
            || jsonb_build_object(
                'default_provider', 'openai',
                'default_model', 'openai/gpt-5.6-sol',
                'default_thinking', COALESCE(
                    memory_model_config->>'default_thinking', 'high'
                )
            )
        """
    )


def downgrade() -> None:
    # Per-cycle model pins are intentionally not restored: they are the
    # configuration this migration retires, and the pre-migration values are
    # recoverable from cycle_revisions when a specific cycle needs one back.
    op.execute(
        """
        UPDATE orgs
        SET memory_model_config = COALESCE(memory_model_config, '{}'::jsonb)
            || jsonb_build_object('default_model', 'openai/gpt-5.5')
        WHERE memory_model_config->>'default_model' = 'openai/gpt-5.6-sol'
        """
    )
