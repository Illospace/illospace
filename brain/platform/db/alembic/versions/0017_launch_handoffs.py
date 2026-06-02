"""Add launch handoffs.

Revision ID: 0017_launch_handoffs
Revises: 0016_thread_urls_object_references
Create Date: 2026-06-02
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0017_launch_handoffs"
down_revision = "0016_thread_urls_object_references"
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
    if _table_exists("launch_handoffs") and not _index_exists("launch_handoffs", name):
        op.create_index(name, "launch_handoffs", columns)


def upgrade() -> None:
    if not _table_exists("launch_handoffs"):
        op.create_table(
            "launch_handoffs",
            sa.Column("id", _uuid_type(), primary_key=True),
            sa.Column("org_id", _uuid_type(), sa.ForeignKey("orgs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("created_by_user_id", _uuid_type(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("source_surface", sa.String(length=40), nullable=False),
            sa.Column("source_ref", _json_type(), nullable=False, server_default=_json_default("{}")),
            sa.Column("target_tool", sa.String(length=40), nullable=False),
            sa.Column("title", sa.Text(), nullable=False),
            sa.Column("summary", sa.Text(), nullable=True),
            sa.Column("instructions", sa.Text(), nullable=False),
            sa.Column("acceptance_criteria", _json_type(), nullable=False, server_default=_json_default("[]")),
            sa.Column("context_parts", _json_type(), nullable=False, server_default=_json_default("[]")),
            sa.Column("repo_origin_url", sa.Text(), nullable=True),
            sa.Column("branch_hint", sa.Text(), nullable=True),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="open"),
            sa.Column("launch_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
            sa.Column(
                "last_launched_by_user_id",
                _uuid_type(),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("last_launched_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("idempotency_key", sa.String(length=120), nullable=True),
            sa.Column("metadata", _json_type(), nullable=False, server_default=_json_default("{}")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.CheckConstraint(
                "status IN ('open', 'launched', 'claimed', 'expired', 'archived')",
                name="ck_launch_handoffs_status",
            ),
            sa.UniqueConstraint("org_id", "idempotency_key", name="uq_launch_handoffs_org_idempotency"),
        )

    _create_index_if_missing(
        "ix_launch_handoffs_org_target_status_created",
        ["org_id", "target_tool", "status", "created_at"],
    )
    _create_index_if_missing("ix_launch_handoffs_org_source_created", ["org_id", "source_surface", "created_at"])
    _create_index_if_missing("ix_launch_handoffs_created_by", ["created_by_user_id", "created_at"])


def downgrade() -> None:
    if not _table_exists("launch_handoffs"):
        return
    for name in (
        "ix_launch_handoffs_created_by",
        "ix_launch_handoffs_org_source_created",
        "ix_launch_handoffs_org_target_status_created",
    ):
        if _index_exists("launch_handoffs", name):
            op.drop_index(name, table_name="launch_handoffs")
    if _constraint_exists("launch_handoffs", "uq_launch_handoffs_org_idempotency"):
        op.drop_constraint("uq_launch_handoffs_org_idempotency", "launch_handoffs", type_="unique")
    op.drop_table("launch_handoffs")
