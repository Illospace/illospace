"""Add Thread URL previews and object references.

Revision ID: 0016_thread_urls_object_references
Revises: 0015_agent_run_source_idempotency
Create Date: 2026-06-01
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0016_thread_urls_object_references"
down_revision = "0015_agent_run_source_idempotency"
branch_labels = None
depends_on = None


def _schema() -> str | None:
    return "public" if op.get_bind().dialect.name == "postgresql" else None


def _inspector():
    return sa.inspect(op.get_bind())


def _table_exists(table_name: str) -> bool:
    return table_name in set(_inspector().get_table_names(schema=_schema()))


def _column_exists(table_name: str, column_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return column_name in {column["name"] for column in _inspector().get_columns(table_name, schema=_schema())}


def _index_exists(table_name: str, index_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return index_name in {index["name"] for index in _inspector().get_indexes(table_name, schema=_schema())}


def upgrade() -> None:
    if _table_exists("ideas"):
        if not _column_exists("ideas", "preview_summary"):
            op.add_column("ideas", sa.Column("preview_summary", sa.Text(), nullable=True))
        if not _column_exists("ideas", "preview_source"):
            op.add_column("ideas", sa.Column("preview_source", sa.String(length=30), nullable=True))
        if not _column_exists("ideas", "preview_updated_at"):
            op.add_column("ideas", sa.Column("preview_updated_at", sa.DateTime(timezone=True), nullable=True))

    if not _table_exists("object_references"):
        op.create_table(
            "object_references",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "org_id",
                postgresql.UUID(as_uuid=False),
                sa.ForeignKey("orgs.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("source_type", sa.String(length=60), nullable=False),
            sa.Column("source_id", sa.String(length=120), nullable=False),
            sa.Column("object_type", sa.String(length=60), nullable=False),
            sa.Column("object_id", sa.String(length=120), nullable=True),
            sa.Column("original_ref", sa.Text(), nullable=False),
            sa.Column("canonical_ref", sa.Text(), nullable=True),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="available"),
            sa.Column("reference_payload", postgresql.JSONB(), nullable=True, server_default=sa.text("'{}'::jsonb")),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("NOW()")),
            sa.UniqueConstraint(
                "source_type",
                "source_id",
                "object_type",
                "object_id",
                "original_ref",
                name="uq_object_references_source_object_ref",
            ),
        )

    if _table_exists("object_references"):
        if not _index_exists("object_references", "ix_object_references_source"):
            op.create_index("ix_object_references_source", "object_references", ["source_type", "source_id"])
        if not _index_exists("object_references", "ix_object_references_object"):
            op.create_index("ix_object_references_object", "object_references", ["object_type", "object_id"])
        if not _index_exists("object_references", "ix_object_references_org_created"):
            op.create_index("ix_object_references_org_created", "object_references", ["org_id", "created_at"])


def downgrade() -> None:
    if _table_exists("object_references"):
        op.drop_table("object_references")
    if _table_exists("ideas"):
        for column_name in ("preview_updated_at", "preview_source", "preview_summary"):
            if _column_exists("ideas", column_name):
                op.drop_column("ideas", column_name)
