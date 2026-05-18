"""Add project profile privacy controls.

Revision ID: 0005_project_profile_privacy
Revises: 0004_external_agent_connections
Create Date: 2026-05-18
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0005_project_profile_privacy"
down_revision = "0004_external_agent_connections"
branch_labels = None
depends_on = None


def _table_exists(table_name: str) -> bool:
    return table_name in set(sa.inspect(op.get_bind()).get_table_names(schema="public"))


def _column_exists(table_name: str, column_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return column_name in {
        column["name"]
        for column in sa.inspect(op.get_bind()).get_columns(table_name, schema="public")
    }


def _index_exists(table_name: str, index_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return index_name in {
        index["name"]
        for index in sa.inspect(op.get_bind()).get_indexes(table_name, schema="public")
    }


def _constraint_exists(table_name: str, constraint_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    inspector = sa.inspect(op.get_bind())
    constraints = inspector.get_check_constraints(table_name, schema="public")
    return constraint_name in {constraint["name"] for constraint in constraints}


def upgrade() -> None:
    if not _column_exists("project_profiles", "visibility"):
        op.add_column(
            "project_profiles",
            sa.Column("visibility", sa.String(length=20), nullable=False, server_default="public"),
        )
        op.alter_column("project_profiles", "visibility", server_default="private")

    if not _index_exists("project_profiles", "ix_project_profiles_org_visibility"):
        op.create_index(
            "ix_project_profiles_org_visibility",
            "project_profiles",
            ["org_id", "visibility"],
        )
    if not _constraint_exists("project_profiles", "ck_project_profiles_visibility"):
        op.create_check_constraint(
            "ck_project_profiles_visibility",
            "project_profiles",
            "visibility IN ('private', 'public')",
        )

    if not _table_exists("project_profile_access"):
        op.create_table(
            "project_profile_access",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "project_profile_id",
                postgresql.UUID(as_uuid=False),
                sa.ForeignKey("project_profiles.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "shared_with_user_id",
                postgresql.UUID(as_uuid=False),
                sa.ForeignKey("users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "shared_by_user_id",
                postgresql.UUID(as_uuid=False),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.UniqueConstraint(
                "project_profile_id",
                "shared_with_user_id",
                name="uq_project_profile_access_profile_user",
            ),
        )
        op.create_index(
            "ix_project_profile_access_user",
            "project_profile_access",
            ["shared_with_user_id"],
        )


def downgrade() -> None:
    if _table_exists("project_profile_access"):
        op.drop_index("ix_project_profile_access_user", table_name="project_profile_access")
        op.drop_table("project_profile_access")
    if _index_exists("project_profiles", "ix_project_profiles_org_visibility"):
        op.drop_index("ix_project_profiles_org_visibility", table_name="project_profiles")
    if _constraint_exists("project_profiles", "ck_project_profiles_visibility"):
        op.drop_constraint("ck_project_profiles_visibility", "project_profiles", type_="check")
    if _column_exists("project_profiles", "visibility"):
        op.drop_column("project_profiles", "visibility")
