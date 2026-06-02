"""Shared extraction, resolution, and persistence for product object references."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.object_reference import ObjectReference
from brain.systems.cortex.thread_links import (
    extract_thread_reference_values,
    thread_id_from_reference,
)
from brain.systems.cortex.thread_read_model import THREAD_OBJECT_TYPE, resolve_thread_reference
from brain.systems.launch_handoffs import (
    LAUNCH_HANDOFF_OBJECT_TYPE,
    extract_launch_handoff_reference_values,
    handoff_id_from_reference,
    resolve_launch_handoff_reference,
)

SOURCE_CHAT_MESSAGE = "chat_message"
SOURCE_THREAD_DISCUSSION_COMMENT = "thread_discussion_comment"
SOURCE_IDEA_THREAD = "idea_thread"

ResolveReference = Callable[
    [AsyncSession, str, str, str, str | None, bool],
    Awaitable[dict[str, Any]],
]


@dataclass(frozen=True)
class ObjectReferenceResolver:
    object_type: str
    extract_values: Callable[[str], list[str]]
    id_from_reference: Callable[[Any, bool], str | None]
    resolve_value: ResolveReference
    object_id_keys: tuple[str, ...]
    canonical_ref_keys: tuple[str, ...]


async def _resolve_thread_value(
    session: AsyncSession,
    value: str,
    object_id: str,
    org_id: str,
    user_id: str | None,
    include_handoff: bool,
) -> dict[str, Any]:
    return await resolve_thread_reference(
        session,
        object_id,
        org_id=org_id,
        user_id=user_id,
        original_ref=value,
        include_handoff=include_handoff,
    )


async def _resolve_launch_handoff_value(
    session: AsyncSession,
    value: str,
    object_id: str,
    org_id: str,
    user_id: str | None,
    include_handoff: bool,
) -> dict[str, Any]:
    return await resolve_launch_handoff_reference(session, value, org_id=org_id)


OBJECT_REFERENCE_RESOLVERS: tuple[ObjectReferenceResolver, ...] = (
    ObjectReferenceResolver(
        object_type=THREAD_OBJECT_TYPE,
        extract_values=extract_thread_reference_values,
        id_from_reference=lambda value, allow_raw_ids: thread_id_from_reference(value, allow_raw_id=allow_raw_ids),
        resolve_value=_resolve_thread_value,
        object_id_keys=("thread_id", "object_id"),
        canonical_ref_keys=("thread_url", "url"),
    ),
    ObjectReferenceResolver(
        object_type=LAUNCH_HANDOFF_OBJECT_TYPE,
        extract_values=extract_launch_handoff_reference_values,
        id_from_reference=lambda value, allow_raw_ids: handoff_id_from_reference(value, allow_raw_id=allow_raw_ids),
        resolve_value=_resolve_launch_handoff_value,
        object_id_keys=("launch_handoff_id", "object_id"),
        canonical_ref_keys=("launch_url", "url"),
    ),
)


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


def extract_object_reference_values(text: str) -> list[str]:
    values: list[str] = []
    for resolver in OBJECT_REFERENCE_RESOLVERS:
        values.extend(resolver.extract_values(text))
    return _dedupe_values(values)


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
    seen_ids: dict[str, set[str]] = {resolver.object_type: set() for resolver in OBJECT_REFERENCE_RESOLVERS}
    for value in _dedupe_values(values):
        for resolver in OBJECT_REFERENCE_RESOLVERS:
            object_id = resolver.id_from_reference(value, allow_raw_ids)
            if not object_id:
                continue
            if object_id not in seen_ids[resolver.object_type]:
                seen_ids[resolver.object_type].add(object_id)
                references.append(
                    await resolver.resolve_value(session, value, object_id, org_id, user_id, include_handoff)
                )
            break
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
        extract_object_reference_values(text),
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


def _resolver_for_payload(reference: dict[str, Any]) -> ObjectReferenceResolver | None:
    object_type = str(reference.get("object_type") or "")
    return next((resolver for resolver in OBJECT_REFERENCE_RESOLVERS if resolver.object_type == object_type), None)


def _reference_value_from_keys(reference: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = reference.get(key)
        if value:
            return str(value)
    return None


def _reference_object_id(reference: dict[str, Any]) -> str | None:
    resolver = _resolver_for_payload(reference)
    keys = resolver.object_id_keys if resolver else ("object_id",)
    return _reference_value_from_keys(reference, keys)


def _reference_canonical_ref(reference: dict[str, Any]) -> str | None:
    resolver = _resolver_for_payload(reference)
    keys = resolver.canonical_ref_keys if resolver else ("url",)
    return _reference_value_from_keys(reference, keys)


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
    reference_values = extract_object_reference_values(text)
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
                object_id=_reference_object_id(reference),
                original_ref=str(reference.get("original_ref") or ""),
                canonical_ref=_reference_canonical_ref(reference),
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
    "extract_object_reference_values",
    "merge_object_reference_metadata",
    "object_references_for_source",
    "resolve_object_reference_values",
    "resolve_object_references_in_text",
    "store_object_references_for_source",
]
