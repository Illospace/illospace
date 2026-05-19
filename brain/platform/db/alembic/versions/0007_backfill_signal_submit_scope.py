"""Backfill signal-submit scope for personal-agent bridge tokens.

Revision ID: 0007_backfill_signal_submit_scope
Revises: 0006_inbound_coordination
Create Date: 2026-05-19
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0007_backfill_signal_submit_scope"
down_revision = "0006_inbound_coordination"
branch_labels = None
depends_on = None


BACKFILL_SQL = """
UPDATE external_agent_connection_tokens AS token
SET scopes = (
    SELECT jsonb_agg(scope ORDER BY scope)
    FROM (
        SELECT DISTINCT value AS scope
        FROM jsonb_array_elements_text(
            CASE
                WHEN jsonb_typeof(token.scopes) = 'array' THEN token.scopes
                ELSE '[]'::jsonb
            END
        ) AS existing(value)
        UNION
        SELECT 'signal:submit'
    ) AS merged
)
FROM external_agent_connections AS connection
WHERE connection.id = token.connection_id
  AND token.revoked_at IS NULL
  AND connection.disabled_at IS NULL
  AND lower(coalesce(connection.status, '')) <> 'disabled'
  AND (
      lower(coalesce(connection.transport, '')) IN ('hosted_mcp', 'bridge_pull')
      OR lower(coalesce(connection.agent_kind, '')) IN (
          'codex',
          'hermes',
          'openclaw',
          'claude-code',
          'opencode'
      )
  )
  AND NOT EXISTS (
      SELECT 1
      FROM jsonb_array_elements_text(
          CASE
              WHEN jsonb_typeof(token.scopes) = 'array' THEN token.scopes
              ELSE '[]'::jsonb
          END
      ) AS scope(value)
      WHERE scope.value = 'signal:submit' OR scope.value = '*'
  )
"""


def _table_exists(table_name: str) -> bool:
    return table_name in set(sa.inspect(op.get_bind()).get_table_names(schema="public"))


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    if not _table_exists("external_agent_connection_tokens"):
        return
    if not _table_exists("external_agent_connections"):
        return
    op.execute(BACKFILL_SQL)


def downgrade() -> None:
    # Do not remove scopes: later user-granted scopes are indistinguishable from
    # this compatibility backfill.
    return None
