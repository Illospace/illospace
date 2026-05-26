"""Default new workspace apps to app capsules.

Revision ID: 0011_app_capsule_workspace_app_defaults
Revises: 0010_thread_context_and_discussion
Create Date: 2026-05-26
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0011_app_capsule_workspace_app_defaults"
down_revision = "0010_thread_context_and_discussion"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in inspector.get_table_names(schema="public")


def upgrade() -> None:
    if _table_exists("workspace_apps"):
        op.alter_column("workspace_apps", "renderer_key", server_default="app-capsule")
    if _table_exists("workspace_app_versions"):
        op.alter_column("workspace_app_versions", "renderer_key", server_default="app-capsule")
        op.alter_column("workspace_app_versions", "source_kind", server_default="html")


def downgrade() -> None:
    if _table_exists("workspace_apps"):
        op.alter_column("workspace_apps", "renderer_key", server_default="generated-ui-app")
    if _table_exists("workspace_app_versions"):
        op.alter_column("workspace_app_versions", "renderer_key", server_default="generated-ui-app")
        op.alter_column("workspace_app_versions", "source_kind", server_default="json")
