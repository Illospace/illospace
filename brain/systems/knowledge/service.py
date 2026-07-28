"""Connector-agnostic knowledge ingestion pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
from typing import Any

import numpy as np
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.kernel.config import KNOWLEDGE_EMBEDDING_DIM
from brain.platform.db.models.knowledge import (
    KnowledgeItem,
    KnowledgeItemEmbedding,
    KnowledgeSyncState,
)
from brain.systems.knowledge.connectors.base import KnowledgeConnector, KnowledgeDraft
from brain.systems.memory import embeddings as embedding_client
from brain.systems.reconstructive_memory.embeddings import embedding_model_identity
from brain.systems.runtime_settings import memory as runtime_settings
from brain.systems.runtime_settings.memory import EmbeddingRuntimeConfig

logger = logging.getLogger(__name__)

RAW_TEXT_MAX_CHARS = 20_000


@dataclass
class KnowledgeSyncStats:
    ingested: int = 0
    skipped: int = 0
    failed: int = 0
    truncated: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "ingested": self.ingested,
            "skipped": self.skipped,
            "failed": self.failed,
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class KnowledgeSyncResult:
    source: str
    status: str
    stats: dict[str, int]
    cursor: dict[str, Any]
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source": self.source,
            "status": self.status,
            "stats": self.stats,
            "cursor": self.cursor,
        }
        if self.error:
            payload["error"] = self.error
        return payload


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        default=str,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def content_digest(draft: KnowledgeDraft, *, raw_text: str, extra: dict[str, Any]) -> str:
    """Hash the content-bearing fields, excluding source watermarks."""

    payload = {
        "kind": draft.kind,
        "title": draft.title,
        "summary": draft.summary,
        "resolution": draft.resolution,
        "entities": draft.entities,
        "raw_text": raw_text,
        "extra": extra,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def build_search_text(
    *,
    title: str,
    summary: str,
    resolution: str | None,
    entities: list[Any],
    raw_text: str,
) -> str:
    values = [
        title,
        summary,
        resolution or "",
        " ".join(str(entity) for entity in entities),
        raw_text,
    ]
    return "\n".join(value.strip() for value in values if value and value.strip())


def _bounded_raw_text(draft: KnowledgeDraft) -> tuple[str, dict[str, Any], bool]:
    raw_text = str(draft.raw_text or "")
    extra = dict(draft.extra or {})
    if len(raw_text) <= RAW_TEXT_MAX_CHARS:
        return raw_text, extra, bool(extra.get("body_truncated"))
    extra.update(
        {
            "raw_text_truncated": True,
            "raw_text_total_chars": len(raw_text),
        }
    )
    return raw_text[:RAW_TEXT_MAX_CHARS], extra, True


async def _upsert_item(
    session: AsyncSession,
    *,
    draft: KnowledgeDraft,
    ingested_at: datetime,
) -> tuple[KnowledgeItem, bool, bool]:
    if draft.source_ref.strip() == "":
        raise ValueError("Knowledge drafts require a stable source_ref")
    raw_text, extra, truncated = _bounded_raw_text(draft)
    digest = content_digest(draft, raw_text=raw_text, extra=extra)
    item = await session.scalar(
        select(KnowledgeItem).where(
            KnowledgeItem.source == draft.source,
            KnowledgeItem.source_ref == draft.source_ref,
        )
    )
    changed = item is None or item.content_digest != digest
    search_text = build_search_text(
        title=draft.title,
        summary=draft.summary,
        resolution=draft.resolution,
        entities=list(draft.entities),
        raw_text=raw_text,
    )
    if item is None:
        item = KnowledgeItem(
            source=draft.source,
            kind=draft.kind,
            source_ref=draft.source_ref,
            title=draft.title,
            summary=draft.summary,
            resolution=draft.resolution,
            entities=list(draft.entities),
            raw_text=raw_text,
            search_text=search_text,
            extra=extra,
            content_digest=digest,
            source_created_at=draft.source_created_at,
            source_updated_at=draft.source_updated_at,
            ingested_at=ingested_at,
            archived_at=draft.archived_at,
        )
        session.add(item)
    elif changed:
        item.kind = draft.kind
        item.title = draft.title
        item.summary = draft.summary
        item.resolution = draft.resolution
        item.entities = list(draft.entities)
        item.raw_text = raw_text
        item.search_text = search_text
        item.extra = extra
        item.content_digest = digest

    # Watermarks and archival state remain fresh even when content is unchanged.
    item.source_created_at = draft.source_created_at
    item.source_updated_at = draft.source_updated_at
    item.ingested_at = ingested_at
    item.archived_at = draft.archived_at
    await session.flush()
    return item, changed, truncated


async def _ensure_embedding(
    session: AsyncSession,
    *,
    item: KnowledgeItem,
    runtime: EmbeddingRuntimeConfig,
) -> bool:
    model = embedding_model_identity(runtime)
    exists = await session.scalar(
        select(KnowledgeItemEmbedding.id).where(
            KnowledgeItemEmbedding.item_id == item.id,
            KnowledgeItemEmbedding.embedding_kind == "summary",
            KnowledgeItemEmbedding.model == model,
            KnowledgeItemEmbedding.content_digest == item.content_digest,
        )
    )
    if exists is not None:
        return False

    vector = np.asarray(
        embedding_client.embed_document(
            f"{item.title}\n{item.summary}",
            runtime_config=runtime,
        ),
        dtype=np.float32,
    ).reshape(-1)
    dimension = int(vector.shape[0])
    if dimension != int(runtime.dimensions) or dimension != KNOWLEDGE_EMBEDDING_DIM:
        raise ValueError(
            "knowledge embedding dimension mismatch "
            f"(returned={dimension}, runtime={runtime.dimensions}, "
            f"database={KNOWLEDGE_EMBEDDING_DIM})"
        )
    session.add(
        KnowledgeItemEmbedding(
            item_id=item.id,
            embedding_kind="summary",
            model=model,
            dimension=dimension,
            embedding=vector.tolist(),
            content_digest=item.content_digest,
        )
    )
    await session.flush()
    return True


async def _sync_state(
    session: AsyncSession,
    *,
    source: str,
    cursor: dict[str, Any],
    status: str,
    stats: KnowledgeSyncStats,
    run_at: datetime,
) -> None:
    state = await session.get(KnowledgeSyncState, source)
    if state is None:
        state = KnowledgeSyncState(source=source)
        session.add(state)
    state.cursor = dict(cursor)
    state.last_run_at = run_at
    state.last_status = status
    state.last_stats = stats.to_dict()
    await session.flush()


async def sync_connector(
    session: AsyncSession,
    connector: KnowledgeConnector,
) -> KnowledgeSyncResult:
    """Run one connector without allowing embedding outages to hide rows."""

    source = str(connector.source_key).strip()
    if not source:
        raise ValueError("Knowledge connectors require source_key")
    run_at = datetime.now(timezone.utc)
    state = await session.get(KnowledgeSyncState, source)
    cursor = dict(state.cursor or {}) if state is not None else {}
    stats = KnowledgeSyncStats()
    try:
        drafts, new_cursor = await connector.enumerate_changed(session, cursor)
    except Exception as exc:
        stats.failed = 1
        await _sync_state(
            session,
            source=source,
            cursor=cursor,
            status="failed",
            stats=stats,
            run_at=run_at,
        )
        logger.exception("Knowledge connector %s enumeration failed", source)
        return KnowledgeSyncResult(
            source=source,
            status="failed",
            stats=stats.to_dict(),
            cursor=cursor,
            error=str(exc),
        )

    runtime: EmbeddingRuntimeConfig | None = None
    runtime_error: Exception | None = None
    try:
        runtime = await runtime_settings.async_get_embedding_runtime_config(
            session,
            include_secret=True,
        )
    except Exception as exc:
        runtime_error = exc
        logger.warning(
            "Knowledge embeddings unavailable for %s; items remain lexical-searchable: %s",
            source,
            exc,
        )

    for draft in drafts:
        if draft.source != source:
            stats.failed += 1
            logger.error(
                "Knowledge connector %s emitted mismatched draft source %s",
                source,
                draft.source,
            )
            continue
        try:
            async with session.begin_nested():
                item, changed, truncated = await _upsert_item(
                    session,
                    draft=draft,
                    ingested_at=run_at,
                )
                if changed:
                    stats.ingested += 1
                else:
                    stats.skipped += 1
                if truncated:
                    stats.truncated += 1
                if runtime is None:
                    raise runtime_error or RuntimeError("embedding runtime unavailable")
                await _ensure_embedding(session, item=item, runtime=runtime)
        except Exception as exc:
            # A nested transaction only surrounds this draft. Re-land the item
            # without its vector if the savepoint rolled back an embedding error.
            try:
                async with session.begin_nested():
                    await _upsert_item(session, draft=draft, ingested_at=run_at)
            except Exception:
                logger.exception(
                    "Knowledge item write failed for %s %s",
                    source,
                    draft.source_ref,
                )
            stats.failed += 1
            logger.warning(
                "Knowledge embedding/write degraded for %s %s; lexical row retained: %s",
                source,
                draft.source_ref,
                exc,
            )

    status = "ok" if stats.failed == 0 else "degraded"
    await _sync_state(
        session,
        source=source,
        cursor=dict(new_cursor),
        status=status,
        stats=stats,
        run_at=run_at,
    )
    return KnowledgeSyncResult(
        source=source,
        status=status,
        stats=stats.to_dict(),
        cursor=dict(new_cursor),
    )


__all__ = [
    "KnowledgeSyncResult",
    "KnowledgeSyncStats",
    "RAW_TEXT_MAX_CHARS",
    "build_search_text",
    "content_digest",
    "sync_connector",
]
