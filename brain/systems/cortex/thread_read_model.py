"""Thread preview read-model helpers."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.agent_run import AgentRunRow
from brain.platform.db.models.idea import Idea
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.cortex.events import publish_safe
from brain.systems.cortex.thread_links import thread_link_payload

THREAD_OBJECT_TYPE = "thread"
PREVIEW_SOURCE_HANDOFF = "handoff"
PREVIEW_SOURCE_DETERMINISTIC = "deterministic"
PREVIEW_SOURCE_UNAVAILABLE = "unavailable"
MAX_PREVIEW_SUMMARY_CHARS = 420
MAX_HANDOFF_CONTEXT_CHARS = 900


def _compact_text(value: Any, *, limit: int) -> str | None:
    text = " ".join(str(value or "").split())
    if not text:
        return None
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 3)].rstrip()}..."


def _checkpoint(summary: dict[str, Any] | None) -> dict[str, Any]:
    payload = summary if isinstance(summary, dict) else {}
    body = payload.get("summary") if isinstance(payload.get("summary"), dict) else payload
    checkpoint = body.get("checkpoint") if isinstance(body, dict) else None
    return checkpoint if isinstance(checkpoint, dict) else {}


def _summary_body(summary: dict[str, Any] | None) -> dict[str, Any]:
    payload = summary if isinstance(summary, dict) else {}
    body = payload.get("summary") if isinstance(payload.get("summary"), dict) else payload
    return body if isinstance(body, dict) else {}


def _idea_id_from_run_thread_id(value: Any) -> str | None:
    text = str(value or "").strip()
    if text.startswith("thread-discussion:"):
        text = text.split(":", 1)[1].strip()
    if not text:
        return None
    try:
        return str(UUID(text))
    except (TypeError, ValueError):
        return None


def preview_summary_from_handoff(summary: dict[str, Any] | None) -> str | None:
    checkpoint = _checkpoint(summary)
    candidates = [
        checkpoint.get("active_objective"),
        checkpoint.get("recent_user_intent"),
        checkpoint.get("current_state"),
        checkpoint.get("summary"),
        checkpoint.get("verification_status"),
    ]
    sentences = [_compact_text(value, limit=260) for value in candidates if value]
    text = " ".join(part for part in sentences if part)
    return _compact_text(text, limit=MAX_PREVIEW_SUMMARY_CHARS)


def deterministic_preview_summary(
    *,
    title: Any = None,
    description: Any = None,
    intent: Any = None,
    source_tool: Any = None,
) -> str | None:
    title_text = _compact_text(title, limit=160)
    description_text = _compact_text(description, limit=260)
    intent_text = _compact_text(intent, limit=260)
    tool_text = _compact_text(source_tool, limit=80)
    if intent_text:
        return _compact_text(f"Current ask: {intent_text}", limit=MAX_PREVIEW_SUMMARY_CHARS)
    if description_text:
        return _compact_text(description_text, limit=MAX_PREVIEW_SUMMARY_CHARS)
    if title_text and tool_text:
        return _compact_text(f"{title_text}. Shared from {tool_text}.", limit=MAX_PREVIEW_SUMMARY_CHARS)
    return title_text


def compact_handoff_context(summary: dict[str, Any] | None) -> dict[str, Any] | None:
    payload = summary if isinstance(summary, dict) else {}
    body = _summary_body(summary)
    checkpoint = _checkpoint(summary)
    if not checkpoint and not payload:
        return None
    context: dict[str, Any] = {}
    for key in ("run_id", "updated_at", "message_count", "session_id"):
        value = payload.get(key) or body.get(key)
        if value is not None:
            context[key] = value

    checkpoint_context: dict[str, Any] = {}
    for key in (
        "active_objective",
        "recent_user_intent",
        "current_state",
        "summary",
        "verification_status",
    ):
        value = checkpoint.get(key)
        if value:
            checkpoint_context[key] = _compact_text(value, limit=MAX_HANDOFF_CONTEXT_CHARS)
    for key in (
        "current_plan",
        "completed_work",
        "decisions",
        "open_questions",
        "risks_or_unknowns",
        "failed_attempts",
        "important_tool_results",
        "files_or_objects_touched",
    ):
        values = _compact_list(checkpoint.get(key), item_limit=320, max_items=8)
        if values:
            checkpoint_context[key] = values
    if checkpoint_context:
        context["checkpoint"] = checkpoint_context
    return context or None


def _compact_list(value: Any, *, item_limit: int, max_items: int) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    compacted: list[str] = []
    for item in items[:max_items]:
        text = _compact_text(item, limit=item_limit)
        if text:
            compacted.append(text)
    return compacted


def unavailable_thread_reference(original_ref: Any = None, thread_id: Any = None) -> dict[str, Any]:
    normalized_thread_id = str(thread_id) if thread_id else None
    payload: dict[str, Any] = {
        "type": "thread_reference",
        "object_type": THREAD_OBJECT_TYPE,
        "object_id": normalized_thread_id,
        "thread_id": normalized_thread_id,
        "status": "unavailable",
        "title": None,
        "preview_summary": None,
        "preview_source": PREVIEW_SOURCE_UNAVAILABLE,
    }
    if original_ref:
        payload["original_ref"] = str(original_ref)
    return payload


async def _latest_handoff_summary_or_none(session: AsyncSession, idea_id: str) -> dict[str, Any] | None:
    try:
        from brain.systems.runs.cortex.handoff_summary import latest_thread_handoff_summary

        summary = await latest_thread_handoff_summary(session, idea_id)
    except Exception:
        return None
    return summary if isinstance(summary, dict) and summary.get("found") else None


async def refresh_thread_read_model(
    session: AsyncSession,
    idea: Idea,
    *,
    preview_summary: str | None,
    preview_source: str,
    publish_update: bool = True,
) -> dict[str, Any] | None:
    summary = _compact_text(preview_summary, limit=MAX_PREVIEW_SUMMARY_CHARS)
    if not summary:
        return None

    now = datetime.now(timezone.utc)
    changed = (
        getattr(idea, "preview_summary", None) != summary
        or getattr(idea, "preview_source", None) != preview_source
    )
    if changed:
        idea.preview_summary = summary
        idea.preview_source = preview_source
        idea.preview_updated_at = now
        await session.flush()

    updated_at = getattr(idea, "preview_updated_at", None)
    if not changed and updated_at is None:
        updated_at = now

    payload = {
        "idea_id": str(idea.id),
        "thread_id": str(idea.id),
        "org_id": str(getattr(idea, "org_id", "") or "") or None,
        "title": getattr(idea, "display_title", None) or getattr(idea, "title", None),
        "preview_summary": summary,
        "preview_source": preview_source,
        "preview_updated_at": updated_at.isoformat() if hasattr(updated_at, "isoformat") else None,
        **thread_link_payload(idea.id),
    }
    if publish_update and changed:
        publish_safe("thread_read_model_updated", payload)
    return payload


async def ensure_thread_preview_read_model(
    session: AsyncSession,
    idea: Idea,
    *,
    lazy_refresh: bool = True,
) -> tuple[str | None, str | None, str | None]:
    existing_summary = _compact_text(getattr(idea, "preview_summary", None), limit=MAX_PREVIEW_SUMMARY_CHARS)
    existing_source = getattr(idea, "preview_source", None)
    existing_updated_at = getattr(idea, "preview_updated_at", None)
    if existing_summary or not lazy_refresh:
        updated = existing_updated_at.isoformat() if hasattr(existing_updated_at, "isoformat") else None
        return existing_summary, existing_source, updated

    handoff = await _latest_handoff_summary_or_none(session, str(idea.id))
    handoff_preview = preview_summary_from_handoff(handoff)
    if handoff_preview:
        payload = await refresh_thread_read_model(
            session,
            idea,
            preview_summary=handoff_preview,
            preview_source=PREVIEW_SOURCE_HANDOFF,
        )
        return (
            payload.get("preview_summary") if payload else handoff_preview,
            PREVIEW_SOURCE_HANDOFF,
            payload.get("preview_updated_at") if payload else None,
        )

    deterministic = deterministic_preview_summary(title=idea.title, description=idea.description)
    if deterministic:
        payload = await refresh_thread_read_model(
            session,
            idea,
            preview_summary=deterministic,
            preview_source=PREVIEW_SOURCE_DETERMINISTIC,
        )
        return (
            payload.get("preview_summary") if payload else deterministic,
            PREVIEW_SOURCE_DETERMINISTIC,
            payload.get("preview_updated_at") if payload else None,
        )
    return None, None, None


async def thread_reference_payload(
    session: AsyncSession,
    idea: Idea,
    *,
    original_ref: Any = None,
    include_handoff: bool = True,
    lazy_refresh: bool = True,
) -> dict[str, Any]:
    preview_summary, preview_source, preview_updated_at = await ensure_thread_preview_read_model(
        session,
        idea,
        lazy_refresh=lazy_refresh,
    )
    payload: dict[str, Any] = {
        "type": "thread_reference",
        "object_type": THREAD_OBJECT_TYPE,
        "object_id": str(idea.id),
        "thread_id": str(idea.id),
        "status": "available",
        "title": getattr(idea, "display_title", None) or getattr(idea, "title", None) or "Untitled thread",
        "preview_summary": preview_summary,
        "preview_source": preview_source,
        "preview_updated_at": preview_updated_at,
        **thread_link_payload(idea.id),
    }
    if original_ref:
        payload["original_ref"] = str(original_ref)
    if include_handoff:
        handoff = await _latest_handoff_summary_or_none(session, str(idea.id))
        compact = compact_handoff_context(handoff)
        if compact:
            payload["handoff"] = compact
    return payload


async def resolve_thread_reference(
    session: AsyncSession,
    thread_id: str,
    *,
    org_id: str,
    user_id: str | None = None,
    original_ref: Any = None,
    include_handoff: bool = True,
) -> dict[str, Any]:
    normalized_thread_id = _idea_id_from_run_thread_id(thread_id)
    if not normalized_thread_id:
        return unavailable_thread_reference(original_ref=original_ref, thread_id=thread_id)
    stmt = select(Idea).where(Idea.id == normalized_thread_id, Idea.archived_at.is_(None))
    if org_id:
        stmt = stmt.where(Idea.org_id == str(org_id))
    idea = await session.scalar(stmt)
    if idea is None:
        return unavailable_thread_reference(original_ref=original_ref, thread_id=thread_id)
    return await thread_reference_payload(
        session,
        idea,
        original_ref=original_ref,
        include_handoff=include_handoff,
    )


async def refresh_thread_read_model_for_run(
    session: AsyncSession,
    run_id: int,
    handoff_summary: dict[str, Any] | None,
) -> dict[str, Any] | None:
    run = await session.get(AgentRunRow, int(run_id))
    if run is None or not getattr(run, "thread_id", None):
        return None
    idea_id = _idea_id_from_run_thread_id(run.thread_id)
    if not idea_id:
        return None
    idea = await session.get(Idea, idea_id)
    if idea is None:
        return None
    preview = preview_summary_from_handoff(handoff_summary)
    if not preview:
        return None
    return await refresh_thread_read_model(
        session,
        idea,
        preview_summary=preview,
        preview_source=PREVIEW_SOURCE_HANDOFF,
    )


async def refresh_thread_read_model_for_run_id(
    run_id: int | None,
    handoff_summary: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if run_id is None:
        return None
    async with UnitOfWork() as uow:
        return await refresh_thread_read_model_for_run(uow.session, int(run_id), handoff_summary)


__all__ = [
    "PREVIEW_SOURCE_DETERMINISTIC",
    "PREVIEW_SOURCE_HANDOFF",
    "THREAD_OBJECT_TYPE",
    "compact_handoff_context",
    "deterministic_preview_summary",
    "ensure_thread_preview_read_model",
    "preview_summary_from_handoff",
    "refresh_thread_read_model",
    "refresh_thread_read_model_for_run",
    "refresh_thread_read_model_for_run_id",
    "resolve_thread_reference",
    "thread_reference_payload",
    "unavailable_thread_reference",
]
