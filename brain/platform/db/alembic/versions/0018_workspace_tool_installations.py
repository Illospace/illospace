"""Add workspace tool installations.

Revision ID: 0018_workspace_tool_installations
Revises: 0017_launch_handoffs
Create Date: 2026-06-04
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0018_workspace_tool_installations"
down_revision = "0017_launch_handoffs"
branch_labels = None
depends_on = None


def _schema() -> str | None:
    return "public" if op.get_bind().dialect.name == "postgresql" else None


def _inspector():
    return sa.inspect(op.get_bind())


def _table_exists(table_name: str) -> bool:
    return table_name in set(_inspector().get_table_names(schema=_schema()))


def _index_exists(table_name: str, index_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return index_name in {index["name"] for index in _inspector().get_indexes(table_name, schema=_schema())}


def _constraint_exists(table_name: str, constraint_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    constraints = _inspector().get_unique_constraints(table_name, schema=_schema())
    constraints += _inspector().get_check_constraints(table_name, schema=_schema())
    constraints += _inspector().get_foreign_keys(table_name, schema=_schema())
    return constraint_name in {constraint["name"] for constraint in constraints}


def _json_default(value: str) -> sa.TextClause:
    return sa.text(f"'{value}'::jsonb") if op.get_bind().dialect.name == "postgresql" else sa.text(f"'{value}'")


def _json_type():
    return postgresql.JSONB() if op.get_bind().dialect.name == "postgresql" else sa.JSON()


def _uuid_type():
    return postgresql.UUID(as_uuid=False) if op.get_bind().dialect.name == "postgresql" else sa.String()


def _create_index_if_missing(name: str, columns: list[str]) -> None:
    if _table_exists("workspace_tool_installations") and not _index_exists("workspace_tool_installations", name):
        op.create_index(name, "workspace_tool_installations", columns)


def upgrade() -> None:
    if not _table_exists("workspace_tool_installations"):
        op.create_table(
            "workspace_tool_installations",
            sa.Column("id", _uuid_type(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
            sa.Column("org_id", _uuid_type(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("bundle_id", sa.String(length=120), nullable=False),
            sa.Column("display_name", sa.String(length=200), nullable=False),
            sa.Column("version", sa.String(length=80), nullable=True),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="requested"),
            sa.Column("install_root", sa.Text(), nullable=True),
            sa.Column("bin_path", sa.Text(), nullable=True),
            sa.Column(
                "requested_by_user_id",
                _uuid_type(),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("requested_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("installed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("install_request", _json_type(), nullable=False, server_default=_json_default("{}")),
            sa.Column("health", _json_type(), nullable=False, server_default=_json_default("{}")),
            sa.Column("metadata", _json_type(), nullable=False, server_default=_json_default("{}")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.CheckConstraint(
                "status IN ('requested', 'queued', 'installing', 'installed', 'failed', 'removed')",
                name="ck_workspace_tool_installations_status",
            ),
            sa.UniqueConstraint("org_id", "bundle_id", name="uq_workspace_tool_installations_org_bundle"),
        )

    _create_index_if_missing("ix_workspace_tool_installations_org_status", ["org_id", "status"])
    _create_index_if_missing(
        "ix_workspace_tool_installations_org_bundle_status",
        ["org_id", "bundle_id", "status"],
    )


def downgrade() -> None:
    if not _table_exists("workspace_tool_installations"):
        return
    for name in (
        "ix_workspace_tool_installations_org_bundle_status",
        "ix_workspace_tool_installations_org_status",
    ):
        if _index_exists("workspace_tool_installations", name):
            op.drop_index(name, table_name="workspace_tool_installations")
    if _constraint_exists("workspace_tool_installations", "uq_workspace_tool_installations_org_bundle"):
        op.drop_constraint(
            "uq_workspace_tool_installations_org_bundle",
            "workspace_tool_installations",
            type_="unique",
        )
    op.drop_table("workspace_tool_installations")
