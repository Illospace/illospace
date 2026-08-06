"""Connector-agnostic knowledge ingestion pipeline."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
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
from brain.systems.knowledge.connectors.base import (
    EnumerationFailure,
    EnumerationFailureKind,
    KNOWLEDGE_SCOPE_EXTRA_KEY,
    KnowledgeConnector,
    KnowledgeDraft,
)
from brain.systems.knowledge.connectors.memory import MemoryConnector
from brain.systems.knowledge.distillation import (
    DISTILLATION_CURSOR_KEY,
    DISTILLATION_MANIFEST_VERSION,
    DISTILLATION_MAX_ATTEMPTS,
    DistillationEntry,
    admit_distillation,
    fallback_draft,
    inspect_distillation,
)
from brain.systems.memory import embeddings as embedding_client
from brain.systems.reconstructive_memory.embeddings import embedding_model_identity
from brain.systems.runtime_settings import memory as runtime_settings
from brain.systems.runtime_settings.memory import EmbeddingRuntimeConfig

logger = logging.getLogger(__name__)

RAW_TEXT_MAX_CHARS = 20_000
_ENUMERATION_ERRORS_KEY = "enumeration_errors"


@dataclass
class KnowledgeSyncStats:
    ingested: int = 0
    skipped: int = 0
    failed: int = 0
    truncated: int = 0
    pending: int = 0
    distilled: int = 0
    config_faults: int = 0

    def to_dict(self) -> dict[str, int]:
        payload = {
            "ingested": self.ingested,
            "skipped": self.skipped,
            "failed": self.failed,
            "truncated": self.truncated,
        }
        if self.pending:
            payload["pending"] = self.pending
        if self.distilled:
            payload["distilled"] = self.distilled
        if self.config_faults:
            payload["config_faults"] = self.config_faults
        return payload


@dataclass(frozen=True)
class KnowledgeSyncResult:
    source: str
    status: str
    stats: dict[str, int]
    cursor: dict[str, Any]
    corpus_empty: bool
    error: str | None = None
    config_faults: tuple[EnumerationFailure, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source": self.source,
            "status": self.status,
            "stats": self.stats,
            "cursor": self.cursor,
            "corpus_empty": self.corpus_empty,
        }
        if self.error:
            payload["error"] = self.error
        if self.config_faults:
            payload["config_faults"] = [
                _enumeration_failure_payload(failure)
                for failure in self.config_faults
            ]
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
    question: str,
    title: str,
    summary: str,
    resolution: str | None,
    entities: list[Any],
    raw_text: str,
) -> str:
    values = [
        question,
        title,
        summary,
        resolution or "",
        " ".join(str(entity) for entity in entities),
        raw_text,
    ]
    return "\n".join(value.strip() for value in values if value and value.strip())


def build_embedding_text(item: KnowledgeItem) -> str:
    """Render every distilled field, while deliberately excluding raw text."""

    extra = dict(item.extra or {})
    distillation = extra.get("distillation")
    question = (
        str(distillation.get("question") or "").strip()
        if isinstance(distillation, dict)
        else ""
    )
    values = [
        question or item.title,
        item.summary,
        item.resolution or "",
        " ".join(str(entity) for entity in item.entities),
    ]
    return "\n".join(value.strip() for value in values if value and value.strip())


def _bounded_raw_text(draft: KnowledgeDraft) -> tuple[str, dict[str, Any], bool]:
    raw_text = str(draft.raw_text or "")
    extra = dict(draft.extra or {})
    if len(raw_text) <= RAW_TEXT_MAX_CHARS:
        return raw_text, extra, bool(
            extra.get("body_truncated") or extra.get("raw_text_truncated")
        )
    extra.update(
        {
            "raw_text_truncated": True,
            "raw_text_total_chars": len(raw_text),
        }
    )
    return raw_text[:RAW_TEXT_MAX_CHARS], extra, True


def _bounded_draft(draft: KnowledgeDraft) -> tuple[KnowledgeDraft, bool]:
    raw_text, extra, truncated = _bounded_raw_text(draft)
    return replace(draft, raw_text=raw_text, extra=extra), truncated


def _distillation_manifest(cursor: dict[str, Any]) -> dict[str, Any] | None:
    value = cursor.get(DISTILLATION_CURSOR_KEY)
    return dict(value) if isinstance(value, dict) else None


def _manifest_cursor(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _manifest_enumeration_failures(value: Any) -> tuple[EnumerationFailure, ...]:
    if not isinstance(value, list):
        return ()
    failures: list[EnumerationFailure] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        scope = str(item.get("scope") or "").strip()
        message = str(item.get("message") or "").strip()
        if scope and message:
            try:
                kind = EnumerationFailureKind(
                    str(item.get("kind") or EnumerationFailureKind.TRANSIENT)
                )
            except ValueError:
                kind = EnumerationFailureKind.TRANSIENT
            failures.append(
                EnumerationFailure(
                    scope=scope,
                    message=message,
                    kind=kind,
                    reason_code=(
                        str(item.get("reason_code") or "").strip() or None
                    ),
                    remediation=(
                        str(item.get("remediation") or "").strip() or None
                    ),
                )
            )
    return tuple(failures)


def _enumeration_failure_payload(failure: EnumerationFailure) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "scope": failure.scope,
        "message": failure.message,
        "kind": failure.kind.value,
    }
    if failure.reason_code:
        payload["reason_code"] = failure.reason_code
    if failure.remediation:
        payload["remediation"] = failure.remediation
    return payload


def _enumeration_error(failures: tuple[EnumerationFailure, ...]) -> str | None:
    return "; ".join(
        f"{failure.scope}: "
        f"{f'[{failure.reason_code}] ' if failure.reason_code else ''}"
        f"{failure.message}"
        for failure in failures
    ) or None


def _account_enumeration_failures(
    stats: KnowledgeSyncStats,
    failures: tuple[EnumerationFailure, ...],
) -> tuple[EnumerationFailure, ...]:
    config_faults = tuple(
        failure
        for failure in failures
        if failure.kind is EnumerationFailureKind.CONFIGURATION
    )
    stats.config_faults += len(config_faults)
    stats.failed += len(failures) - len(config_faults)
    return config_faults


def _sync_status(stats: KnowledgeSyncStats) -> str:
    return "ok" if stats.failed == 0 and stats.config_faults == 0 else "degraded"


def _pending_cursor(
    *,
    committed_cursor: dict[str, Any],
    proposed_cursor: dict[str, Any],
    entries: list[dict[str, Any]],
    enumeration_failures: tuple[EnumerationFailure, ...] = (),
) -> dict[str, Any]:
    manifest = {
        "version": DISTILLATION_MANIFEST_VERSION,
        "committed_cursor": dict(committed_cursor),
        "proposed_cursor": dict(proposed_cursor),
        "entries": entries,
    }
    if enumeration_failures:
        manifest[_ENUMERATION_ERRORS_KEY] = [
            _enumeration_failure_payload(failure)
            for failure in enumeration_failures
        ]
    return {
        **committed_cursor,
        DISTILLATION_CURSOR_KEY: manifest,
    }


async def _start_distillations(
    session: AsyncSession,
    drafts: list[KnowledgeDraft],
) -> tuple[list[dict[str, Any]], list[KnowledgeDraft], int]:
    entries: list[dict[str, Any]] = []
    fallbacks: list[KnowledgeDraft] = []
    skipped = 0
    existing_by_ref: dict[str, KnowledgeItem] = {}
    embedded_item_ids: set[int] = set()
    if drafts:
        source_refs = [draft.source_ref for draft in drafts]
        existing_items = list(
            (
                await session.scalars(
                    select(KnowledgeItem).where(
                        KnowledgeItem.source == drafts[0].source,
                        KnowledgeItem.source_ref.in_(source_refs),
                    )
                )
            ).all()
        )
        existing_by_ref = {item.source_ref: item for item in existing_items}
        if existing_items:
            try:
                runtime = await runtime_settings.async_get_embedding_runtime_config(
                    session,
                    include_secret=True,
                )
                model = embedding_model_identity(runtime)
            except Exception:
                model = None
            if model is not None:
                embedded_item_ids = set(
                    (
                        await session.scalars(
                            select(KnowledgeItemEmbedding.item_id)
                            .join(
                                KnowledgeItem,
                                KnowledgeItem.id == KnowledgeItemEmbedding.item_id,
                            )
                            .where(
                                KnowledgeItem.id.in_(
                                    [item.id for item in existing_items]
                                ),
                                KnowledgeItemEmbedding.model == model,
                                KnowledgeItemEmbedding.content_digest
                                == KnowledgeItem.content_digest,
                            )
                        )
                    ).all()
                )
    for draft in drafts:
        digest = content_digest(
            draft,
            raw_text=draft.raw_text,
            extra=dict(draft.extra or {}),
        )
        existing = existing_by_ref.get(draft.source_ref)
        distillation = (
            dict(existing.extra or {}).get("distillation")
            if existing is not None
            else None
        )
        if (
            isinstance(distillation, dict)
            and str(distillation.get("input_digest") or "") == digest
            and str(distillation.get("status") or "") in {"completed", "failed"}
            and (
                str(distillation.get("status") or "") == "failed"
                or existing.id in embedded_item_ids
            )
        ):
            skipped += 1
            continue
        try:
            entry = await admit_distillation(
                session,
                draft,
                input_digest=digest,
                attempt=1,
            )
        except Exception as exc:
            logger.exception(
                "Knowledge distillation admission failed for %s %s",
                draft.source,
                draft.source_ref,
            )
            fallbacks.append(
                fallback_draft(
                    draft,
                    input_digest=digest,
                    attempt=1,
                    error=str(exc),
                )
            )
            continue
        entries.append(entry.to_dict())
    return entries, fallbacks, skipped


async def _harvest_distillations(
    session: AsyncSession,
    raw_entries: list[Any],
) -> tuple[list[dict[str, Any]], list[tuple[KnowledgeDraft, bool]], int]:
    pending: list[dict[str, Any]] = []
    resolved: list[tuple[KnowledgeDraft, bool]] = []
    failures = 0
    for raw_entry in raw_entries:
        if not isinstance(raw_entry, dict):
            failures += 1
            logger.error("Ignoring malformed knowledge distillation manifest entry")
            continue
        try:
            entry = DistillationEntry.from_mapping(raw_entry)
            outcome = await inspect_distillation(session, entry)
        except Exception as exc:
            logger.exception("Knowledge distillation harvest failed")
            failures += 1
            continue
        else:
            error = str(outcome.error or "")

        if outcome is not None and outcome.status == "completed" and outcome.draft:
            resolved.append((outcome.draft, True))
            continue
        if outcome is not None and outcome.status == "pending":
            pending.append(entry.to_dict())
            continue

        retry_draft = outcome.draft if outcome is not None else None
        if retry_draft is None:
            failures += 1
            logger.error(
                "Knowledge distillation run %s cannot be retried without its draft snapshot",
                entry.run_id,
            )
            continue
        if entry.attempt < DISTILLATION_MAX_ATTEMPTS:
            try:
                replacement = await admit_distillation(
                    session,
                    retry_draft,
                    input_digest=entry.input_digest,
                    attempt=entry.attempt + 1,
                )
            except Exception as exc:
                error = str(exc)
            else:
                pending.append(replacement.to_dict())
                continue

        failures += 1
        resolved.append(
            (
                fallback_draft(
                    retry_draft,
                    input_digest=entry.input_digest,
                    attempt=entry.attempt,
                    error=error or "knowledge distillation attempts exhausted",
                ),
                False,
            )
        )
    return pending, resolved, failures


async def _upsert_item(
    session: AsyncSession,
    *,
    draft: KnowledgeDraft,
    ingested_at: datetime,
) -> tuple[KnowledgeItem, bool, bool]:
    if draft.source_ref.strip() == "":
        raise ValueError("Knowledge drafts require a stable source_ref")
    raw_text, extra, truncated = _bounded_raw_text(draft)
    extra[KNOWLEDGE_SCOPE_EXTRA_KEY] = draft.scope.value
    digest = content_digest(draft, raw_text=raw_text, extra=extra)
    item = await session.scalar(
        select(KnowledgeItem).where(
            KnowledgeItem.source == draft.source,
            KnowledgeItem.source_ref == draft.source_ref,
        )
    )
    changed = item is None or item.content_digest != digest
    distillation = extra.get("distillation")
    question = (
        str(distillation.get("question") or "").strip()
        if isinstance(distillation, dict)
        else ""
    )
    search_text = build_search_text(
        question=question,
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
            build_embedding_text(item),
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
    stats: dict[str, int],
    run_at: datetime,
) -> None:
    state = await session.get(KnowledgeSyncState, source)
    if state is None:
        state = KnowledgeSyncState(source=source)
        session.add(state)
    state.cursor = dict(cursor)
    state.last_run_at = run_at
    state.last_status = status
    state.last_stats = dict(stats)
    await session.flush()


async def _finalize_sync(
    session: AsyncSession,
    *,
    source: str,
    state_cursor: dict[str, Any],
    result_cursor: dict[str, Any],
    status: str,
    stats: KnowledgeSyncStats,
    run_at: datetime,
    error: str | None = None,
    config_faults: tuple[EnumerationFailure, ...] = (),
) -> KnowledgeSyncResult:
    """Persist one sync outcome and expose its current searchable corpus state."""

    stats_payload = stats.to_dict()
    active_item_id = await session.scalar(
        select(KnowledgeItem.id)
        .where(
            KnowledgeItem.source == source,
            KnowledgeItem.archived_at.is_(None),
        )
        .limit(1)
    )
    corpus_empty = active_item_id is None
    await _sync_state(
        session,
        source=source,
        cursor=state_cursor,
        status=status,
        stats=stats_payload,
        run_at=run_at,
    )
    return KnowledgeSyncResult(
        source=source,
        status=status,
        stats=stats_payload,
        cursor=result_cursor,
        corpus_empty=corpus_empty,
        error=error,
        config_faults=config_faults,
    )


async def _ingest_drafts(
    session: AsyncSession,
    *,
    source: str,
    drafts: list[tuple[KnowledgeDraft, bool]],
    stats: KnowledgeSyncStats,
    run_at: datetime,
) -> None:
    if not drafts:
        return
    runtime: EmbeddingRuntimeConfig | None = None
    runtime_error: Exception | None = None
    if any(should_embed for _draft, should_embed in drafts):
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

    for draft, should_embed in drafts:
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
                if should_embed:
                    if runtime is None:
                        raise runtime_error or RuntimeError(
                            "embedding runtime unavailable"
                        )
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


async def index_memory_node(
    session: AsyncSession,
    *,
    node_id: int,
) -> KnowledgeSyncStats:
    """Upsert one committed memory node without advancing the sweep cursor."""

    stats = KnowledgeSyncStats()
    connector = MemoryConnector(max_items=1)
    draft = await connector.draft_for_node(
        session,
        node_id=node_id,
    )
    if draft is None:
        stats.skipped = 1
        return stats

    bounded_draft, _ = _bounded_draft(draft)
    await _ingest_drafts(
        session,
        source=connector.source_key,
        drafts=[(bounded_draft, True)],
        stats=stats,
        run_at=datetime.now(timezone.utc),
    )
    return stats


async def sync_connector(
    session: AsyncSession,
    connector: KnowledgeConnector,
) -> KnowledgeSyncResult:
    """Run one connector with restart-safe distillation and honest accounting."""

    source = str(connector.source_key).strip()
    if not source:
        raise ValueError("Knowledge connectors require source_key")
    run_at = datetime.now(timezone.utc)
    state = await session.get(KnowledgeSyncState, source)
    stored_cursor = dict(state.cursor or {}) if state is not None else {}
    stats = KnowledgeSyncStats()
    manifest = _distillation_manifest(stored_cursor)
    if manifest is not None:
        committed_cursor = _manifest_cursor(manifest.get("committed_cursor"))
        proposed_cursor = _manifest_cursor(manifest.get("proposed_cursor"))
        enumeration_failures = _manifest_enumeration_failures(
            manifest.get(_ENUMERATION_ERRORS_KEY)
        )
        enumeration_error = _enumeration_error(enumeration_failures)
        pending, resolved, failures = await _harvest_distillations(
            session,
            list(manifest.get("entries") or []),
        )
        stats.failed += failures
        config_faults = _account_enumeration_failures(
            stats,
            enumeration_failures,
        )
        stats.distilled = sum(
            1 for _draft, should_embed in resolved if should_embed
        )
        await _ingest_drafts(
            session,
            source=source,
            drafts=resolved,
            stats=stats,
            run_at=run_at,
        )
        if pending:
            stats.pending = len(pending)
            return await _finalize_sync(
                session,
                source=source,
                state_cursor=_pending_cursor(
                    committed_cursor=committed_cursor,
                    proposed_cursor=proposed_cursor,
                    entries=pending,
                    enumeration_failures=enumeration_failures,
                ),
                result_cursor=committed_cursor,
                status="pending",
                stats=stats,
                run_at=run_at,
                error=enumeration_error,
                config_faults=config_faults,
            )

        status = _sync_status(stats)
        return await _finalize_sync(
            session,
            source=source,
            state_cursor=proposed_cursor,
            result_cursor=proposed_cursor,
            status=status,
            stats=stats,
            run_at=run_at,
            error=enumeration_error,
            config_faults=config_faults,
        )

    cursor = dict(stored_cursor)
    try:
        enumeration = await connector.enumerate_changed(session, cursor)
        drafts = enumeration.drafts
        new_cursor = enumeration.cursor
        enumeration_failures = enumeration.failures
    except Exception as exc:
        stats.failed = 1
        logger.exception("Knowledge connector %s enumeration failed", source)
        return await _finalize_sync(
            session,
            source=source,
            state_cursor=cursor,
            result_cursor=cursor,
            status="failed",
            stats=stats,
            run_at=run_at,
            error=str(exc),
        )

    enumeration_error = _enumeration_error(enumeration_failures)
    config_faults = _account_enumeration_failures(stats, enumeration_failures)

    structural: list[tuple[KnowledgeDraft, bool]] = []
    distillable: list[KnowledgeDraft] = []
    for original_draft in drafts:
        draft, truncated = _bounded_draft(original_draft)
        if draft.source != source:
            stats.failed += 1
            logger.error(
                "Knowledge connector %s emitted mismatched draft source %s",
                source,
                draft.source,
            )
            continue
        if draft.distill:
            if truncated:
                stats.truncated += 1
            distillable.append(draft)
        else:
            structural.append((draft, True))

    await _ingest_drafts(
        session,
        source=source,
        drafts=structural,
        stats=stats,
        run_at=run_at,
    )
    entries, admission_fallbacks, unchanged_distillations = (
        await _start_distillations(session, distillable)
    )
    stats.skipped += unchanged_distillations
    if entries:
        stats.failed += len(admission_fallbacks)
        stats.pending = len(entries)
        await _ingest_drafts(
            session,
            source=source,
            drafts=[(draft, False) for draft in admission_fallbacks],
            stats=stats,
            run_at=run_at,
        )
        return await _finalize_sync(
            session,
            source=source,
            state_cursor=_pending_cursor(
                committed_cursor=cursor,
                proposed_cursor=dict(new_cursor),
                entries=entries,
                enumeration_failures=enumeration_failures,
            ),
            result_cursor=cursor,
            status="pending",
            stats=stats,
            run_at=run_at,
            error=enumeration_error,
            config_faults=config_faults,
        )

    if admission_fallbacks:
        stats.failed += len(admission_fallbacks)
        await _ingest_drafts(
            session,
            source=source,
            drafts=[(draft, False) for draft in admission_fallbacks],
            stats=stats,
            run_at=run_at,
        )

    status = _sync_status(stats)
    return await _finalize_sync(
        session,
        source=source,
        state_cursor=dict(new_cursor),
        result_cursor=dict(new_cursor),
        status=status,
        stats=stats,
        run_at=run_at,
        error=enumeration_error,
        config_faults=config_faults,
    )


__all__ = [
    "KnowledgeSyncResult",
    "KnowledgeSyncStats",
    "RAW_TEXT_MAX_CHARS",
    "build_embedding_text",
    "build_search_text",
    "content_digest",
    "index_memory_node",
    "sync_connector",
]
