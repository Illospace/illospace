"""Add the derived knowledge index.

Revision ID: 0047_knowledge_index
Revises: 0046_cycle_failure_guard
Create Date: 2026-07-28
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

try:
    from pgvector.sqlalchemy import Vector
except ImportError:  # pragma: no cover - migration fallback for non-pg test envs
    Vector = sa.Text

from brain.kernel.config import KNOWLEDGE_EMBEDDING_DIM


revision = "0047_knowledge_index"
down_revision = "0046_cycle_failure_guard"
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
    return index_name in {
        index["name"]
        for index in _inspector().get_indexes(table_name, schema=_schema())
    }


def _json_type():
    return postgresql.JSONB() if op.get_bind().dialect.name == "postgresql" else sa.JSON()


def _json_default(value: str):
    if op.get_bind().dialect.name == "postgresql":
        return sa.text(f"'{value}'::jsonb")
    return sa.text(f"'{value}'")


def _now_default():
    return sa.text("NOW()") if op.get_bind().dialect.name == "postgresql" else sa.text("CURRENT_TIMESTAMP")


def _create_index_if_missing(
    table_name: str,
    index_name: str,
    columns: list[str],
    **kwargs,
) -> None:
    if _table_exists(table_name) and not _index_exists(table_name, index_name):
        op.create_index(index_name, table_name, columns, **kwargs)


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    if not _table_exists("knowledge_items"):
        op.create_table(
            "knowledge_items",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("source", sa.String(length=80), nullable=False),
            sa.Column("kind", sa.String(length=80), nullable=False),
            sa.Column("source_ref", sa.String(length=500), nullable=False),
            sa.Column("title", sa.Text(), nullable=False),
            sa.Column("summary", sa.Text(), nullable=False),
            sa.Column("resolution", sa.Text(), nullable=True),
            sa.Column("entities", _json_type(), nullable=False, server_default=_json_default("[]")),
            sa.Column("raw_text", sa.Text(), nullable=False),
            sa.Column("search_text", sa.Text(), nullable=False, server_default=""),
            sa.Column("extra", _json_type(), nullable=False, server_default=_json_default("{}")),
            sa.Column("content_digest", sa.String(length=128), nullable=False),
            sa.Column("source_created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False, server_default=_now_default()),
            sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("source", "source_ref", name="uq_knowledge_items_source_ref"),
        )

    if not _table_exists("knowledge_item_embeddings"):
        op.create_table(
            "knowledge_item_embeddings",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "item_id",
                sa.Integer(),
                sa.ForeignKey("knowledge_items.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("embedding_kind", sa.String(length=40), nullable=False),
            sa.Column("model", sa.String(length=120), nullable=False),
            sa.Column("dimension", sa.Integer(), nullable=False),
            sa.Column("embedding", Vector(KNOWLEDGE_EMBEDDING_DIM), nullable=True),
            sa.Column("content_digest", sa.String(length=128), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=_now_default()),
            sa.UniqueConstraint(
                "item_id",
                "embedding_kind",
                "model",
                "content_digest",
                name="uq_knowledge_item_embeddings_digest",
            ),
        )

    if not _table_exists("knowledge_sync_state"):
        op.create_table(
            "knowledge_sync_state",
            sa.Column("source", sa.String(length=80), primary_key=True),
            sa.Column("cursor", _json_type(), nullable=False, server_default=_json_default("{}")),
            sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_status", sa.String(length=40), nullable=True),
            sa.Column("last_stats", _json_type(), nullable=False, server_default=_json_default("{}")),
        )

    _create_index_if_missing("knowledge_items", "ix_knowledge_items_source", ["source"])
    _create_index_if_missing("knowledge_items", "ix_knowledge_items_kind", ["kind"])
    if op.get_bind().dialect.name == "postgresql":
        _create_index_if_missing(
            "knowledge_items",
            "ix_knowledge_items_search_text_trgm",
            ["search_text"],
            postgresql_using="gin",
            postgresql_ops={"search_text": "gin_trgm_ops"},
        )
    else:
        _create_index_if_missing(
            "knowledge_items",
            "ix_knowledge_items_search_text_trgm",
            ["search_text"],
        )
    _create_index_if_missing(
        "knowledge_item_embeddings",
        "ix_knowledge_item_embeddings_item_kind",
        ["item_id", "embedding_kind"],
    )


def downgrade() -> None:
    # Derived index rows are disposable, but broad destructive migration
    # downgrades require an explicit review exception in this repository.
    return None
