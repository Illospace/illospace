"""Derived, source-backed knowledge index persistence models."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    func,
    text as sql_text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

try:
    from pgvector.sqlalchemy import Vector
except ImportError:  # pragma: no cover - pgvector optional in test env
    from sqlalchemy import PickleType as Vector  # type: ignore[assignment]

from brain.kernel.config import KNOWLEDGE_EMBEDDING_DIM
from brain.platform.db.base import Base, CreatedAtMixin

__all__ = [
    "KnowledgeItem",
    "KnowledgeItemEmbedding",
    "KnowledgeSyncState",
]


def _json_column(
    default_sql: str,
    default_factory,
    *,
    nullable: bool = False,
):
    return mapped_column(
        JSONB().with_variant(JSON, "sqlite"),
        nullable=nullable,
        server_default=sql_text(default_sql),
        default=default_factory,
    )


class KnowledgeItem(Base):
    """One disposable index row with provenance to its canonical source."""

    __tablename__ = "knowledge_items"
    __table_args__ = (
        UniqueConstraint(
            "source",
            "source_ref",
            name="uq_knowledge_items_source_ref",
        ),
        Index(
            "ix_knowledge_items_search_text_trgm",
            "search_text",
            postgresql_using="gin",
            postgresql_ops={"search_text": "gin_trgm_ops"},
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    source_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    resolution: Mapped[str | None] = mapped_column(Text, nullable=True)
    entities: Mapped[list[Any]] = _json_column("'[]'", list)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    search_text: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    extra: Mapped[dict[str, Any]] = _json_column("'{}'", dict)
    content_digest: Mapped[str] = mapped_column(String(128), nullable=False)
    source_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    source_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    archived_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class KnowledgeItemEmbedding(Base, CreatedAtMixin):
    """Versioned embeddings separated from disposable item identity."""

    __tablename__ = "knowledge_item_embeddings"
    __table_args__ = (
        Index(
            "ix_knowledge_item_embeddings_item_kind",
            "item_id",
            "embedding_kind",
        ),
        UniqueConstraint(
            "item_id",
            "embedding_kind",
            "model",
            "content_digest",
            name="uq_knowledge_item_embeddings_digest",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("knowledge_items.id", ondelete="CASCADE"),
        nullable=False,
    )
    embedding_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list | None] = mapped_column(
        Vector(KNOWLEDGE_EMBEDDING_DIM),
        nullable=True,
    )
    content_digest: Mapped[str] = mapped_column(String(128), nullable=False)


class KnowledgeSyncState(Base):
    """Watermark and load-bearing accounting for one connector source."""

    __tablename__ = "knowledge_sync_state"

    source: Mapped[str] = mapped_column(String(80), primary_key=True)
    cursor: Mapped[dict[str, Any]] = _json_column("'{}'", dict)
    last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    last_status: Mapped[str | None] = mapped_column(String(40), nullable=True)
    last_stats: Mapped[dict[str, Any]] = _json_column("'{}'", dict)
