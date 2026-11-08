"""Add organization MCP servers and metadata-only audit history.

Revision ID: 0071_external_mcp_servers
Revises: 0070_tool_policies
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql as pg

revision = "0071_external_mcp_servers"
down_revision = "0070_tool_policies"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The public baseline creates current model tables before later revisions replay.
    existing = set(sa.inspect(op.get_bind()).get_table_names())
    if "external_mcp_servers" not in existing:
        op.create_table(
            "external_mcp_servers",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("org_id", pg.UUID(as_uuid=False), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("name", sa.String(32), nullable=False),
            sa.Column("url", sa.Text, nullable=False),
            sa.Column("transport", sa.String(24), nullable=False, server_default="streamable_http"),
            sa.Column("auth_mode", sa.String(24), nullable=False, server_default="none"),
            sa.Column("bearer_token_key", sa.String(128)),
            sa.Column("token_url", sa.Text),
            sa.Column("client_id_key", sa.String(128)),
            sa.Column("client_secret_key", sa.String(128)),
            sa.Column("scope", sa.Text),
            sa.Column("resource", sa.Text),
            sa.Column("allowed_tools", pg.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
            sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
            sa.Column("timeout_seconds", sa.Integer, nullable=False, server_default="30"),
            sa.Column("created_by", pg.UUID(as_uuid=False), sa.ForeignKey("users.id", ondelete="SET NULL")),
            sa.Column("updated_by", pg.UUID(as_uuid=False), sa.ForeignKey("users.id", ondelete="SET NULL")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.UniqueConstraint("org_id", "name", name="uq_external_mcp_servers_org_name"),
            sa.CheckConstraint("timeout_seconds BETWEEN 1 AND 120", name="ck_external_mcp_timeout"),
            sa.CheckConstraint("transport = 'streamable_http'", name="ck_external_mcp_transport"),
            sa.CheckConstraint("auth_mode IN ('none', 'bearer', 'client_credentials')", name="ck_external_mcp_auth"),
        )
        op.create_index("ix_external_mcp_servers_org_id", "external_mcp_servers", ["org_id"])
    if "external_mcp_audit_events" not in existing:
        op.create_table(
            "external_mcp_audit_events",
            sa.Column("id", sa.Integer, primary_key=True),
            sa.Column("org_id", pg.UUID(as_uuid=False), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("event_type", sa.String(32), nullable=False),
            sa.Column("server_id", sa.Integer, sa.ForeignKey("external_mcp_servers.id", ondelete="SET NULL")),
            sa.Column("server_name", sa.String(32), nullable=False),
            sa.Column("actor_user_id", pg.UUID(as_uuid=False), sa.ForeignKey("users.id", ondelete="SET NULL")),
            sa.Column("run_id", sa.Integer, sa.ForeignKey("agent_runs.id", ondelete="SET NULL")),
            sa.Column("tool_name", sa.String(128)),
            sa.Column("outcome", sa.String(32), nullable=False),
            sa.Column("duration_ms", sa.Integer),
            sa.Column("changed_fields", pg.JSONB),
            sa.Column("error_kind", sa.String(64)),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
        )
        op.create_index("ix_external_mcp_audit_events_org_id", "external_mcp_audit_events", ["org_id"])


def downgrade() -> None:
    op.drop_table("external_mcp_audit_events")
    op.drop_table("external_mcp_servers")
