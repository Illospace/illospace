"""Add external personal-agent connections.

Revision ID: 0004_external_agent_connections
Revises: 0003_schema_simplification
Create Date: 2026-05-14
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0004_external_agent_connections"
down_revision = "0003_schema_simplification"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "external_agent_connections",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("agent_kind", sa.String(length=40), nullable=False),
        sa.Column("transport", sa.String(length=60), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="pending"),
        sa.Column("endpoint_url", sa.Text(), nullable=True),
        sa.Column("remote_agent_id", sa.Text(), nullable=True),
        sa.Column("remote_session_key", sa.Text(), nullable=True),
        sa.Column("remote_agent_card", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("capabilities", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("auth_secret_ref", sa.Text(), nullable=True),
        sa.Column("auth_metadata", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index(
        "ix_external_agent_connections_org_owner_status",
        "external_agent_connections",
        ["org_id", "owner_user_id", "status"],
    )
    op.create_index(
        "ix_external_agent_connections_org_kind",
        "external_agent_connections",
        ["org_id", "agent_kind"],
    )
    op.create_index(
        "ix_external_agent_connections_status_seen",
        "external_agent_connections",
        ["status", "last_seen_at"],
    )

    op.create_table(
        "external_agent_connection_tokens",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("connection_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("external_agent_connections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("org_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_user_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.String(length=96), nullable=False),
        sa.Column("token_prefix", sa.String(length=24), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("scopes", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("token_hash", name="uq_external_agent_connection_tokens_hash"),
    )
    op.create_index(
        "ix_external_agent_connection_tokens_connection",
        "external_agent_connection_tokens",
        ["connection_id"],
    )
    op.create_index(
        "ix_external_agent_connection_tokens_org_owner",
        "external_agent_connection_tokens",
        ["org_id", "owner_user_id"],
    )

    op.create_table(
        "external_agent_tasks",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("org_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("connection_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("external_agent_connections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("source_surface", sa.String(length=40), nullable=False),
        sa.Column("source_idea_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("ideas.id", ondelete="SET NULL"), nullable=True),
        sa.Column("source_thread_message_id", sa.Integer(), nullable=True),
        sa.Column("source_chat_conversation_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("chat_conversations.id", ondelete="SET NULL"), nullable=True),
        sa.Column("source_chat_message_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("input_parts", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="queued"),
        sa.Column("remote_task_id", sa.Text(), nullable=True),
        sa.Column("remote_run_id", sa.Text(), nullable=True),
        sa.Column("remote_session_id", sa.Text(), nullable=True),
        sa.Column("illo_run_id", sa.Integer(), sa.ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("idempotency_key", sa.String(length=80), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result_summary", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.UniqueConstraint("connection_id", "idempotency_key", name="uq_external_agent_tasks_connection_idempotency"),
    )
    op.create_index(
        "ix_external_agent_tasks_org_status_created",
        "external_agent_tasks",
        ["org_id", "status", "created_at"],
    )
    op.create_index(
        "ix_external_agent_tasks_connection_status_created",
        "external_agent_tasks",
        ["connection_id", "status", "created_at"],
    )
    op.create_index(
        "ix_external_agent_tasks_source_idea_created",
        "external_agent_tasks",
        ["source_idea_id", "created_at"],
    )
    op.create_index("ix_external_agent_tasks_remote_task", "external_agent_tasks", ["remote_task_id"])
    op.create_index("ix_external_agent_tasks_illo_run", "external_agent_tasks", ["illo_run_id"])

    op.create_table(
        "external_agent_task_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("external_agent_tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("org_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("connection_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("external_agent_connections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("remote_event_id", sa.Text(), nullable=True),
        sa.Column("producer", sa.String(length=80), nullable=False),
        sa.Column("visibility", sa.String(length=30), nullable=False, server_default="public"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        sa.UniqueConstraint("task_id", "sequence_no", name="uq_external_agent_task_events_task_sequence"),
    )
    op.create_index(
        "ix_external_agent_task_events_task_created",
        "external_agent_task_events",
        ["task_id", "created_at"],
    )
    op.create_index(
        "ix_external_agent_task_events_org_created",
        "external_agent_task_events",
        ["org_id", "created_at"],
    )
    op.create_index(
        "ix_external_agent_task_events_connection_created",
        "external_agent_task_events",
        ["connection_id", "created_at"],
    )

    op.create_table(
        "external_agent_task_artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=False), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("task_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("external_agent_tasks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("org_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("connection_id", postgresql.UUID(as_uuid=False), sa.ForeignKey("external_agent_connections.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("mime_type", sa.Text(), nullable=True),
        sa.Column("content_text", sa.Text(), nullable=True),
        sa.Column("content_json", postgresql.JSONB(), nullable=True),
        sa.Column("uri", sa.Text(), nullable=True),
        sa.Column("upload_id", sa.Text(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
    )
    op.create_index(
        "ix_external_agent_task_artifacts_task_created",
        "external_agent_task_artifacts",
        ["task_id", "created_at"],
    )
    op.create_index(
        "ix_external_agent_task_artifacts_org_created",
        "external_agent_task_artifacts",
        ["org_id", "created_at"],
    )


def downgrade() -> None:
    # Public release migrations are forward-only in practice; keeping downgrade
    # non-destructive also preserves the repo's migration guardrails.
    return None
