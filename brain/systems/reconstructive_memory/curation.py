"""Deliberate curation operations for the reconstructive memory graph."""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.reconstructive_memory import MemoryEdgeNode, MemoryNode
from brain.platform.db.repositories.reconstructive_memory import (
    EdgeDraft,
    MemoryAssertionRepository,
    MemoryEdgeRepository,
    MemoryNodeRepository,
    MemorySourceRepository,
    ReconstructiveMemoryCompatibilityRepository,
    SourceSpanDraft,
)
from brain.systems.reconstructive_memory.ingestion import ingest_memory_source

CURATION_CREATED_BY = "agent_curation"
MEMORY_CURATOR_SOURCE_KIND = "memory_curator"
SUPERSEDED_BY_EDGE = "superseded_by"


async def link_memories(
    session: AsyncSession,
    *,
    source_node_id: int,
    target_node_id: int,
    relationship: str,
    reason: str,
    user_id: str,
    org_id: str | None,
    run_id: int | str | None = None,
) -> dict[str, Any]:
    """Create or reinforce one deliberate, source-backed memory edge."""

    if source_node_id == target_node_id:
        raise ValueError("memory_link requires two different nodes")
    nodes = await _visible_nodes_exact(
        session,
        [source_node_id, target_node_id],
        user_id=user_id,
        org_id=org_id,
    )
    normalized_relationship = _normalize_relationship(relationship)
    normalized_reason = _normalize_reason(reason)
    visibility = _curation_visibility(nodes)
    curation_source, spans = await _record_curation_source(
        session,
        action="link",
        reason=normalized_reason,
        payload={
            "source_node": source_node_id,
            "target_node": target_node_id,
            "relationship": normalized_relationship,
        },
        user_id=user_id,
        org_id=org_id,
        visibility=visibility,
        run_id=run_id,
    )
    edge = await MemoryEdgeRepository(session).upsert_edge(
        draft=EdgeDraft(
            source_node_id=source_node_id,
            target_node_id=target_node_id,
            edge_kind=normalized_relationship,
            confidence=1.0,
            evidence_span_ids=(spans[0].id,),
            created_by=CURATION_CREATED_BY,
        ),
        org_id=org_id,
        visibility=visibility,
    )
    return {
        "memory_system": "reconstructive",
        "action": "link",
        "edge_id": edge.id,
        "source_node": source_node_id,
        "target_node": target_node_id,
        "relationship": normalized_relationship,
        "created_by": edge.created_by,
        "reason": normalized_reason,
        "curation_source_id": curation_source.id,
    }


async def supersede_memory(
    session: AsyncSession,
    *,
    old_node_id: int,
    reason: str,
    user_id: str,
    org_id: str | None,
    new_content: str | None = None,
    new_node_id: int | None = None,
    run_id: int | str | None = None,
) -> dict[str, Any]:
    """Replace one visible memory with a new or existing visible node."""

    if (new_content is None) == (new_node_id is None):
        raise ValueError("memory_supersede requires exactly one of new_content or new_node")
    normalized_reason = _normalize_reason(reason)
    old_node = (
        await _visible_nodes_exact(
            session,
            [old_node_id],
            user_id=user_id,
            org_id=org_id,
        )
    )[0]
    existing_target = await session.scalar(
        select(MemoryEdgeNode.target_node_id).where(
            MemoryEdgeNode.source_node_id == old_node_id,
            MemoryEdgeNode.edge_kind == SUPERSEDED_BY_EDGE,
        )
    )
    if existing_target is not None and (new_node_id is None or existing_target != new_node_id):
        raise ValueError(f"Memory {old_node_id} is already superseded by node {existing_target}")

    replacement_source_id: int | None = None
    if new_content is not None:
        cleaned_content = " ".join(new_content.split()).strip()
        if len(cleaned_content) < 20:
            raise ValueError("new_content is too short (min 20 chars)")
        ingested = await ingest_memory_source(
            session,
            content=cleaned_content,
            content_kind=old_node.content_kind or "episode",
            source_kind=CURATION_CREATED_BY,
            source_ref=f"{run_id or 'direct'}:supersede:{old_node_id}",
            org_id=old_node.org_id or org_id,
            user_id=user_id,
            visibility=old_node.visibility,
            scope_key=f"supersedes:{old_node_id}",
            confidence=max(0.5, float(old_node.confidence or 0.5)),
            evidence={
                "curation_action": "supersede",
                "old_node": old_node_id,
                "reason": normalized_reason,
                "created_by": CURATION_CREATED_BY,
            },
            authority_principal=user_id,
        )
        new_node_id = ingested.content_node_id
        replacement_source_id = ingested.source_id

    assert new_node_id is not None
    if old_node_id == new_node_id:
        raise ValueError("A memory cannot supersede itself")
    nodes = await _visible_nodes_exact(
        session,
        [old_node_id, new_node_id],
        user_id=user_id,
        org_id=org_id,
    )
    visibility = _curation_visibility(nodes)
    curation_source, spans = await _record_curation_source(
        session,
        action="supersede",
        reason=normalized_reason,
        payload={"old_node": old_node_id, "new_node": new_node_id},
        user_id=user_id,
        org_id=org_id,
        visibility=visibility,
        run_id=run_id,
    )
    await MemoryNodeRepository(session).mark_superseded(old_node_id)
    await MemoryAssertionRepository(session).mark_superseded_for_node(old_node_id)
    edge = await MemoryEdgeRepository(session).upsert_edge(
        draft=EdgeDraft(
            source_node_id=old_node_id,
            target_node_id=new_node_id,
            edge_kind=SUPERSEDED_BY_EDGE,
            confidence=1.0,
            evidence_span_ids=(spans[0].id,),
            created_by=CURATION_CREATED_BY,
        ),
        org_id=org_id,
        visibility=visibility,
    )
    return {
        "memory_system": "reconstructive",
        "action": "supersede",
        "old_node": old_node_id,
        "new_node": new_node_id,
        "edge_id": edge.id,
        "created_by": edge.created_by,
        "reason": normalized_reason,
        "curation_source_id": curation_source.id,
        "replacement_source_id": replacement_source_id,
    }


async def archive_memories(
    session: AsyncSession,
    *,
    node_ids: Sequence[int],
    reason: str,
    user_id: str,
    org_id: str | None,
    run_id: int | str | None = None,
) -> dict[str, Any]:
    """Archive visible memory nodes and persist the reason as source evidence."""

    unique_ids = list(dict.fromkeys(int(node_id) for node_id in node_ids))
    if not unique_ids:
        raise ValueError("memory_archive requires at least one node id")
    nodes = await _visible_nodes_exact(
        session,
        unique_ids,
        user_id=user_id,
        org_id=org_id,
    )
    normalized_reason = _normalize_reason(reason)
    curation_source, _ = await _record_curation_source(
        session,
        action="archive",
        reason=normalized_reason,
        payload={"node_ids": unique_ids},
        user_id=user_id,
        org_id=org_id,
        visibility=_curation_visibility(nodes),
        run_id=run_id,
    )
    await ReconstructiveMemoryCompatibilityRepository(session).archive_many(unique_ids)
    return {
        "memory_system": "reconstructive",
        "action": "archive",
        "node_ids": unique_ids,
        "archived_count": len(unique_ids),
        "created_by": CURATION_CREATED_BY,
        "reason": normalized_reason,
        "curation_source_id": curation_source.id,
    }


async def archive_memory_by_policy(
    session: AsyncSession,
    *,
    node: MemoryNode,
    rule: str,
    policy_version: str,
    run_id: int | str,
    reason: str,
) -> dict[str, Any]:
    """Soft-archive one selected node with complete curator audit evidence."""

    if node.archived_at is not None:
        raise ValueError(f"Memory node {node.id} is already archived")
    normalized_rule = _normalize_audit_field(rule, field="rule")
    normalized_policy_version = _normalize_audit_field(
        policy_version,
        field="policy_version",
    )
    normalized_reason = _normalize_reason(reason)
    audit_text = (
        f"Rule: {normalized_rule}. Policy version: {normalized_policy_version}. "
        f"Target node: {node.id}. Run ID: {run_id}. Reason: {normalized_reason}"
    )
    curation_source, spans = await MemorySourceRepository(session).create_with_spans(
        source_kind=MEMORY_CURATOR_SOURCE_KIND,
        source_ref=f"nightly-memory-maintenance:{run_id}:archive:{node.id}",
        raw_content=audit_text,
        spans=[
            SourceSpanDraft(
                text=audit_text,
                locator={
                    "kind": "memory_curation",
                    "action": "archive",
                    "rule": normalized_rule,
                    "policy_version": normalized_policy_version,
                    "target_node": node.id,
                    "run_id": str(run_id),
                },
            )
        ],
        org_id=node.org_id,
        user_id=node.user_id,
        visibility=node.visibility,
        structured_payload={
            "action": "archive",
            "rule": normalized_rule,
            "policy_version": normalized_policy_version,
            "target_node": node.id,
            "run_id": str(run_id),
            "reason": normalized_reason,
            "created_by": MEMORY_CURATOR_SOURCE_KIND,
        },
        authority_principal=MEMORY_CURATOR_SOURCE_KIND,
        sensitivity=node.sensitivity,
    )
    await ReconstructiveMemoryCompatibilityRepository(session).archive_many([node.id])
    return {
        "node_id": node.id,
        "curation_source_id": curation_source.id,
        "curation_span_id": spans[0].id,
    }


async def _visible_nodes_exact(
    session: AsyncSession,
    node_ids: Sequence[int],
    *,
    user_id: str,
    org_id: str | None,
) -> list[MemoryNode]:
    unique_ids = list(dict.fromkeys(int(node_id) for node_id in node_ids))
    nodes = await MemoryNodeRepository(session).get_visible_nodes(
        unique_ids,
        user_id=user_id,
        org_id=org_id,
    )
    found_ids = {node.id for node in nodes}
    missing = [node_id for node_id in unique_ids if node_id not in found_ids]
    if missing:
        raise LookupError(f"Memory nodes are missing, archived, or not visible: {missing}")
    return nodes


async def _record_curation_source(
    session: AsyncSession,
    *,
    action: str,
    reason: str,
    payload: dict[str, Any],
    user_id: str,
    org_id: str | None,
    visibility: str,
    run_id: int | str | None,
):
    return await MemorySourceRepository(session).create_with_spans(
        source_kind=CURATION_CREATED_BY,
        source_ref=f"{run_id or 'direct'}:{action}:{uuid.uuid4()}",
        raw_content=reason,
        spans=[SourceSpanDraft(text=reason, locator={"kind": "curation_reason", "action": action})],
        org_id=org_id,
        user_id=user_id,
        visibility=visibility,
        structured_payload={
            "action": action,
            "reason": reason,
            "created_by": CURATION_CREATED_BY,
            "run_id": str(run_id) if run_id is not None else None,
            **payload,
        },
        authority_principal=user_id,
    )


def _curation_visibility(nodes: Sequence[MemoryNode]) -> str:
    visibilities = {node.visibility for node in nodes}
    if "private" in visibilities:
        return "private"
    if "team" in visibilities:
        return "team"
    return "org"


def _normalize_relationship(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower()).strip("_")
    if not normalized:
        raise ValueError("relationship is required")
    if len(normalized) > 60:
        raise ValueError("relationship must be at most 60 normalized characters")
    return normalized


def _normalize_reason(value: str) -> str:
    normalized = " ".join(str(value or "").split()).strip()
    if len(normalized) < 3:
        raise ValueError("reason must contain at least 3 characters")
    return normalized


def _normalize_audit_field(value: str, *, field: str) -> str:
    normalized = " ".join(str(value or "").split()).strip()
    if not normalized:
        raise ValueError(f"{field} is required")
    return normalized
