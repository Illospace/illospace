"""Add per-user workspace tool configs.

Revision ID: 0019_workspace_tool_user_configs
Revises: 0018_workspace_tool_installations
Create Date: 2026-06-05
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0019_workspace_tool_user_configs"
down_revision = "0018_workspace_tool_installations"
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
    constraints += _inspector().get_foreign_keys(table_name, schema=_schema())
    return constraint_name in {constraint["name"] for constraint in constraints}


def _json_default(value: str) -> sa.TextClause:
    return sa.text(f"'{value}'::jsonb") if op.get_bind().dialect.name == "postgresql" else sa.text(f"'{value}'")


def _json_type():
    return postgresql.JSONB() if op.get_bind().dialect.name == "postgresql" else sa.JSON()


def _uuid_type():
    return postgresql.UUID(as_uuid=False) if op.get_bind().dialect.name == "postgresql" else sa.String()


def _create_index_if_missing(name: str, columns: list[str]) -> None:
    if _table_exists("workspace_tool_user_configs") and not _index_exists("workspace_tool_user_configs", name):
        op.create_index(name, "workspace_tool_user_configs", columns)


def upgrade() -> None:
    if not _table_exists("workspace_tool_user_configs"):
        op.create_table(
            "workspace_tool_user_configs",
            sa.Column("id", _uuid_type(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
            sa.Column("org_id", _uuid_type(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("user_id", _uuid_type(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("bundle_id", sa.String(length=120), nullable=False),
            sa.Column("preferences", _json_type(), nullable=False, server_default=_json_default("{}")),
            sa.Column("credential_refs", _json_type(), nullable=False, server_default=_json_default("{}")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.UniqueConstraint(
                "org_id",
                "user_id",
                "bundle_id",
                name="uq_workspace_tool_user_configs_org_user_bundle",
            ),
        )

    _create_index_if_missing("ix_workspace_tool_user_configs_org_user", ["org_id", "user_id"])
    _create_index_if_missing(
        "ix_workspace_tool_user_configs_org_bundle",
        ["org_id", "bundle_id"],
    )


def downgrade() -> None:
    if not _table_exists("workspace_tool_user_configs"):
        return
    for name in (
        "ix_workspace_tool_user_configs_org_bundle",
        "ix_workspace_tool_user_configs_org_user",
    ):
        if _index_exists("workspace_tool_user_configs", name):
            op.drop_index(name, table_name="workspace_tool_user_configs")
    if _constraint_exists("workspace_tool_user_configs", "uq_workspace_tool_user_configs_org_user_bundle"):
        op.drop_constraint(
            "uq_workspace_tool_user_configs_org_user_bundle",
            "workspace_tool_user_configs",
            type_="unique",
        )
