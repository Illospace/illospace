"""Shared extraction, resolution, and persistence for product object references."""
from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.object_reference import ObjectReference
from brain.systems.cortex.thread_links import (
    extract_thread_reference_values,
    thread_id_from_reference,
)
from brain.systems.cortex.thread_read_model import THREAD_OBJECT_TYPE, resolve_thread_reference

SOURCE_CHAT_MESSAGE = "chat_message"
SOURCE_THREAD_DISCUSSION_COMMENT = "thread_discussion_comment"
SOURCE_IDEA_THREAD = "idea_thread"


def _dedupe_values(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values or []:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _thread_ref_metadata(references: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [ref for ref in references if ref.get("object_type") == THREAD_OBJECT_TYPE]


async def resolve_object_reference_values(
    session: AsyncSession,
    values: list[Any],
    *,
    org_id: str,
    user_id: str | None = None,
    allow_raw_ids: bool = False,
    include_handoff: bool = True,
) -> list[dict[str, Any]]:
    references: list[dict[str, Any]] = []
    seen_thread_ids: set[str] = set()
    for value in _dedupe_values(values):
        thread_id = thread_id_from_reference(value, allow_raw_id=allow_raw_ids)
        if not thread_id or thread_id in seen_thread_ids:
            continue
        seen_thread_ids.add(thread_id)
        references.append(
            await resolve_thread_reference(
                session,
                thread_id,
                org_id=org_id,
                user_id=user_id,
                original_ref=value,
                include_handoff=include_handoff,
            )
        )
    return references


async def resolve_object_references_in_text(
    session: AsyncSession,
    text: str,
    *,
    org_id: str,
    user_id: str | None = None,
    include_handoff: bool = True,
) -> list[dict[str, Any]]:
    return await resolve_object_reference_values(
        session,
        extract_thread_reference_values(text),
        org_id=org_id,
        user_id=user_id,
        include_handoff=include_handoff,
    )


def merge_object_reference_metadata(
    metadata: dict[str, Any] | None,
    references: list[dict[str, Any]],
) -> dict[str, Any]:
    next_metadata = dict(metadata or {})
    next_metadata["object_references"] = list(references or [])
    next_metadata["thread_references"] = _thread_ref_metadata(references)
    return next_metadata


async def store_object_references_for_source(
    session: AsyncSession,
    *,
    source_type: str,
    source_id: str | int,
    org_id: str,
    text: str,
    user_id: str | None = None,
    include_handoff: bool = True,
) -> list[dict[str, Any]]:
    reference_values = extract_thread_reference_values(text)
    if not reference_values:
        return []

    references = await resolve_object_reference_values(
        session,
        reference_values,
        org_id=org_id,
        user_id=user_id,
        include_handoff=include_handoff,
    )
    await session.execute(
        delete(ObjectReference).where(
            ObjectReference.source_type == source_type,
            ObjectReference.source_id == str(source_id),
        )
    )
    for reference in references:
        session.add(
            ObjectReference(
                org_id=str(org_id),
                source_type=source_type,
                source_id=str(source_id),
                object_type=str(reference.get("object_type") or THREAD_OBJECT_TYPE),
                object_id=str(reference.get("thread_id")) if reference.get("thread_id") else None,
                original_ref=str(reference.get("original_ref") or ""),
                canonical_ref=reference.get("thread_url") or reference.get("url"),
                status=str(reference.get("status") or "available"),
                reference_payload=reference,
            )
        )
    return references


async def object_references_for_source(
    session: AsyncSession,
    *,
    source_type: str,
    source_id: str | int,
) -> list[dict[str, Any]]:
    result = await session.scalars(
        select(ObjectReference)
        .where(
            ObjectReference.source_type == source_type,
            ObjectReference.source_id == str(source_id),
        )
        .order_by(ObjectReference.id.asc())
    )
    return [dict(row.reference_payload or {}) for row in result.all()]


__all__ = [
    "SOURCE_CHAT_MESSAGE",
    "SOURCE_IDEA_THREAD",
    "SOURCE_THREAD_DISCUSSION_COMMENT",
    "merge_object_reference_metadata",
    "object_references_for_source",
    "resolve_object_reference_values",
    "resolve_object_references_in_text",
    "store_object_references_for_source",
]
