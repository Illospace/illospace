"""Add Thread context submissions and Discussion comments.

Revision ID: 0010_thread_context_and_discussion
Revises: 0009_org_owned_provider_credentials
Create Date: 2026-05-21
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0010_thread_context_and_discussion"
down_revision = "0009_org_owned_provider_credentials"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names(schema="public")


def upgrade() -> None:
    if not _table_exists("thread_context_submissions"):
        op.create_table(
            "thread_context_submissions",
            sa.Column(
                "id",
                postgresql.UUID(as_uuid=False),
                primary_key=True,
                server_default=sa.text("gen_random_uuid()"),
            ),
            sa.Column(
                "thread_id",
                postgresql.UUID(as_uuid=False),
                sa.ForeignKey("ideas.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "org_id",
                postgresql.UUID(as_uuid=False),
                sa.ForeignKey("orgs.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "source_connection_id",
                postgresql.UUID(as_uuid=False),
                sa.ForeignKey("external_agent_connections.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "submitted_by_user_id",
                postgresql.UUID(as_uuid=False),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column(
                "inbound_event_id",
                postgresql.UUID(as_uuid=False),
                sa.ForeignKey("inbound_events.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("intent", sa.Text(), nullable=True),
            sa.Column("source", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("constraints", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("correlation", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("parts", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
            sa.Column("routing_result", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        )
        op.create_index(
            "ix_thread_context_submissions_thread_created",
            "thread_context_submissions",
            ["thread_id", "created_at", "id"],
        )
        op.create_index(
            "ix_thread_context_submissions_inbound_event",
            "thread_context_submissions",
            ["inbound_event_id"],
        )

    if not _table_exists("thread_discussion_comments"):
        op.create_table(
            "thread_discussion_comments",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "thread_id",
                postgresql.UUID(as_uuid=False),
                sa.ForeignKey("ideas.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "org_id",
                postgresql.UUID(as_uuid=False),
                sa.ForeignKey("orgs.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "author_user_id",
                postgresql.UUID(as_uuid=False),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("author_kind", sa.String(length=20), nullable=False, server_default="user"),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("attachments", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
            sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        )
        op.create_index(
            "ix_thread_discussion_comments_thread_created",
            "thread_discussion_comments",
            ["thread_id", "created_at", "id"],
        )


def downgrade() -> None:
    return None
