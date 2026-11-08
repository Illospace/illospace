"""Add organization and skill tool policies with change history.

Revision ID: 0070_tool_policies
Revises: 0069_codex_credential_health
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0070_tool_policies"
down_revision = "0069_codex_credential_health"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("tool_policies"):
        op.create_table(
            "tool_policies",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "org_id",
                postgresql.UUID(as_uuid=False),
                sa.ForeignKey("orgs.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("scope_type", sa.String(20), nullable=False),
            sa.Column("scope_key", sa.String(50), nullable=False),
            sa.Column("disabled_tools", postgresql.JSONB(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column(
                "updated_by",
                postgresql.UUID(as_uuid=False),
                sa.ForeignKey("users.id"),
                nullable=False,
            ),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "org_id", "scope_type", "scope_key", name="uq_tool_policy_scope"
            ),
            sa.CheckConstraint(
                "(scope_type = 'organization' AND scope_key = '') OR "
                "(scope_type = 'skill' AND scope_key <> '')",
                name="ck_tool_policy_scope",
            ),
        )
    if not inspector.has_table("tool_policy_change_audits"):
        op.create_table(
            "tool_policy_change_audits",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "org_id",
                postgresql.UUID(as_uuid=False),
                sa.ForeignKey("orgs.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("scope_type", sa.String(20), nullable=False),
            sa.Column("scope_key", sa.String(50), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("actor_id", sa.String(100), nullable=False),
            sa.Column("actor_type", sa.String(30), nullable=False),
            sa.Column("before_disabled_tools", postgresql.JSONB(), nullable=False),
            sa.Column("after_disabled_tools", postgresql.JSONB(), nullable=False),
            sa.Column("changed_at", sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint(
                "org_id",
                "scope_type",
                "scope_key",
                "version",
                name="uq_tool_policy_audit_version",
            ),
        )


def downgrade() -> None:
    raise NotImplementedError(
        "Tool policy audit history must be preserved. This migration has no destructive downgrade."
    )
