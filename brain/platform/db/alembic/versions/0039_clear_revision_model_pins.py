"""Retire per-cycle model pins in the revision ledger too.

Migration 0038 cleared `cycles.model_override`, but scheduled runs resolve
their routing from the revision snapshot bound to the run — which is built
from the newest `cycle_revisions` row, not the live cycle. A cleared cycle
with a pinned latest revision therefore still routed to the pinned model.

Revisions are an immutable ledger, so this records a new revision per affected
cycle (carrying the current cycle configuration with no model pin) instead of
rewriting history. Prior pins stay readable in earlier revisions.

Revision ID: 0039_clear_revision_model_pins
Revises: 0038_single_model_effort_routing
Create Date: 2026-07-24
"""

from __future__ import annotations

from alembic import op


revision = "0039_clear_revision_model_pins"
down_revision = "0038_single_model_effort_routing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO cycle_revisions (
            cycle_id, revision_number, source_type, source_id, rationale,
            name, prompt, schedule_expr, timezone, enabled,
            model_override, thinking_override, target_idea_id, context_policy,
            created_at
        )
        SELECT
            c.id,
            latest.revision_number + 1,
            'system',
            NULL,
            'Retire per-cycle model pinning: Illo routes one model and varies effort only.',
            c.name, c.prompt, c.schedule_expr, c.timezone, c.enabled,
            NULL,
            c.thinking_override,
            c.target_idea_id,
            latest.context_policy,
            now()
        FROM cycles c
        JOIN LATERAL (
            SELECT revision_number, model_override, context_policy
            FROM cycle_revisions
            WHERE cycle_id = c.id
            ORDER BY revision_number DESC, id DESC
            LIMIT 1
        ) latest ON true
        WHERE c.deleted_at IS NULL
          AND latest.model_override IS NOT NULL
          AND btrim(latest.model_override) <> ''
        """
    )


def downgrade() -> None:
    # The recorded revisions are ledger history; leave them in place. Restoring
    # a specific pin means writing a new revision, not deleting this one.
    return None
