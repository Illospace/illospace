"""Bounded mirror of shared source-backed memory into Illo Knowledge.

Knowledge search does not yet enforce per-user memory visibility.  This
connector therefore mirrors only ``org`` and ``team`` content nodes; private
memory remains exclusively owned by the memory subsystem until the knowledge
index has an ACL-aware read path.  The mirror is derived and additive: it reads
``MemoryNode`` rows and never participates in memory recall or mutation.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.kernel.config import KNOWLEDGE_CONNECTOR_BATCH_SIZE
from brain.platform.db.models.knowledge import KnowledgeItem
from brain.platform.db.models.reconstructive_memory import MemoryNode
from brain.platform.db.repositories.reconstructive_memory import memory_node_visibility_predicate
from brain.systems.knowledge.connectors.base import (
    KnowledgeDraft,
    KnowledgeEnumeration,
    KnowledgeScope,
    UpdatedAtCursor,
)
from brain.systems.knowledge.memory_eligibility import (
    MemoryIndexExclusionReason,
    _KNOWLEDGE_NODE_KINDS,
    _SHARED_VISIBILITIES,
    _is_superseded,
    _load_superseded_by,
    memory_index_exclusion_reason,
    memory_node_index_exclusion_reason,
)

logger = logging.getLogger(__name__)


def _knowledge_memory_access_predicate(*, org_id: str, user_id: str | None):
    """Require org access; callers without a user never learn private nodes exist."""

    access = and_(
        MemoryNode.org_id == org_id,
        memory_node_visibility_predicate(org_id=org_id, user_id=user_id),
    )
    if not user_id:
        access = and_(access, MemoryNode.visibility.in_(_SHARED_VISIBILITIES))
    return access


async def get_memory_index_exclusion_reasons(
    session: AsyncSession,
    node_ids: Mapping[str, int],
    *,
    org_id: str,
    user_id: str | None,
) -> dict[str, MemoryIndexExclusionReason]:
    """Explain readable memory handles using eligibility fields only."""

    if not node_ids:
        return {}
    nodes = (await session.execute(
        select(
            MemoryNode.id,
            MemoryNode.node_kind,
            MemoryNode.visibility,
            MemoryNode.archived_at,
            MemoryNode.truth_status,
        ).where(
            MemoryNode.id.in_(node_ids.values()),
            _knowledge_memory_access_predicate(org_id=org_id, user_id=user_id),
        )
    )).all()
    by_id = {node.id: node for node in nodes}
    readable_refs = [ref for ref, node_id in node_ids.items() if node_id in by_id]
    if not readable_refs:
        return {}
    superseded_by = await _load_superseded_by(session, list(by_id))
    mirrors = dict((await session.execute(
        select(KnowledgeItem.source_ref, KnowledgeItem.archived_at).where(
            KnowledgeItem.source == MemoryConnector.source_key,
            KnowledgeItem.source_ref.in_(readable_refs),
        )
    )).all())
    reasons = {}
    for ref in readable_refs:
        node_id = node_ids[ref]
        reason = memory_index_exclusion_reason(
            by_id[node_id],
            superseded_by=superseded_by.get(node_id),
            mirror_archived=mirrors.get(ref) is not None,
            has_mirror=ref in mirrors,
        )
        if reason is not None:
            reasons[ref] = reason
    return reasons


def _candidate_node_query():
    return select(MemoryNode).where(
        MemoryNode.node_kind.in_(_KNOWLEDGE_NODE_KINDS)
    )


def _required_org_id(node: MemoryNode) -> str:
    if node.org_id is None:
        raise ValueError(f"Memory node {node.id} has no organization")
    return str(node.org_id)


def _draft_for_memory(
    node: MemoryNode,
    *,
    superseded_by: int | None,
) -> KnowledgeDraft:
    content = str(node.text or node.canonical_label).strip()
    memory_kind = str(node.content_kind or node.node_kind).strip()
    scope = str(node.scope_key or "default").strip()
    superseded = _is_superseded(node, superseded_by)
    reason = memory_index_exclusion_reason(node, superseded_by=superseded_by)
    archived_at = node.archived_at or (
        node.updated_at
        if reason == MemoryIndexExclusionReason.ARCHIVED_OR_SUPERSEDED
        else None
    )
    return KnowledgeDraft(
        source="memory",
        kind="memory",
        source_ref=f"memory_node:{node.id}",
        scope=KnowledgeScope.ORGANIZATION,
        title=str(node.canonical_label).strip(),
        summary=content,
        entities=list(dict.fromkeys((memory_kind, scope))),
        raw_text=content,
        extra={
            "archived": node.archived_at is not None,
            "confidence": float(node.confidence or 0.0),
            "freshness_status": node.freshness_status,
            "memory_type": memory_kind,
            "node_kind": node.node_kind,
            "org_id": _required_org_id(node),
            "scope": scope,
            "sensitivity": node.sensitivity,
            "source_backed": True,
            "source_type": "reconstructive_memory_node",
            "superseded": superseded,
            "superseded_by": superseded_by,
            "truth_status": node.truth_status,
            "visibility": node.visibility,
        },
        source_created_at=node.created_at,
        source_updated_at=node.updated_at,
        archived_at=archived_at,
    )


def _withdrawn_draft(node: MemoryNode, *, org_id: str) -> KnowledgeDraft:
    """Scrub a formerly shared mirror after its source becomes private."""

    return KnowledgeDraft(
        source="memory",
        kind="memory",
        source_ref=f"memory_node:{node.id}",
        scope=KnowledgeScope.ORGANIZATION,
        title="Memory no longer shared",
        summary="This memory is no longer shared with the workspace.",
        raw_text="",
        extra={
            "archived": True,
            "mirror_status": "visibility_withdrawn",
            "node_kind": node.node_kind,
            "org_id": org_id,
            "truth_status": node.truth_status,
            "visibility": node.visibility,
        },
        source_created_at=node.created_at,
        source_updated_at=node.updated_at,
        archived_at=node.archived_at or node.updated_at,
    )


class MemoryConnector:
    """Enumerate shared source-backed memory content by update watermark."""

    source_key = "memory"

    def __init__(self, *, max_items: int = KNOWLEDGE_CONNECTOR_BATCH_SIZE):
        self.max_items = max(1, int(max_items))

    async def draft_for_node(
        self,
        session: AsyncSession,
        *,
        node_id: int,
    ) -> KnowledgeDraft | None:
        """Build one immediate-index draft with the sweep's eligibility rules."""

        node = await session.scalar(
            _candidate_node_query().where(MemoryNode.id == node_id)
        )
        if node is None:
            return None
        drafts = await self._drafts_for_rows(session, [node])
        return drafts[0] if drafts else None

    async def _drafts_for_rows(
        self,
        session: AsyncSession,
        rows: list[MemoryNode],
    ) -> list[KnowledgeDraft]:
        if not rows:
            return []
        source_refs = [f"memory_node:{node.id}" for node in rows]
        existing_org_ids = {
            source_ref: extra["org_id"]
            for source_ref, extra in (
                await session.execute(
                    select(KnowledgeItem.source_ref, KnowledgeItem.extra).where(
                        KnowledgeItem.source == self.source_key,
                        KnowledgeItem.source_ref.in_(source_refs),
                    )
                )
            ).all()
        }
        candidate_rows = [
            node
            for node in rows
            if node.visibility in _SHARED_VISIBILITIES
            or f"memory_node:{node.id}" in existing_org_ids
        ]
        draft_rows: list[MemoryNode] = []
        active_rows: list[MemoryNode] = []
        for node in candidate_rows:
            if node.visibility in _SHARED_VISIBILITIES:
                if node.org_id is None:
                    logger.warning(
                        "Memory knowledge enumeration skipped node %s: org_id is missing",
                        node.id,
                    )
                    continue
                active_rows.append(node)
            draft_rows.append(node)
        superseded_by = await _load_superseded_by(
            session, [node.id for node in active_rows]
        )
        return [
            _draft_for_memory(node, superseded_by=superseded_by.get(node.id))
            if node.visibility in _SHARED_VISIBILITIES
            else _withdrawn_draft(
                node,
                org_id=existing_org_ids[f"memory_node:{node.id}"],
            )
            for node in draft_rows
        ]

    async def enumerate_changed(
        self,
        session: AsyncSession,
        cursor: dict[str, Any],
    ) -> KnowledgeEnumeration:
        watermark = UpdatedAtCursor.from_mapping(cursor)
        statement = (
            _candidate_node_query()
            .order_by(MemoryNode.updated_at.asc(), MemoryNode.id.asc())
            .limit(self.max_items)
        )
        changed_after = watermark.changed_after(MemoryNode.updated_at, MemoryNode.id)
        if changed_after is not None:
            statement = statement.where(changed_after)

        rows = list((await session.scalars(statement)).all())
        if not rows:
            return KnowledgeEnumeration(drafts=[], cursor=dict(cursor))
        drafts = await self._drafts_for_rows(session, rows)
        last = rows[-1]
        return KnowledgeEnumeration(
            drafts=drafts,
            cursor=watermark.advanced_to(last.updated_at, last.id),
        )


__all__ = [
    "MemoryConnector",
    "MemoryIndexExclusionReason",
    "get_memory_index_exclusion_reasons",
    "memory_index_exclusion_reason",
    "memory_node_index_exclusion_reason",
]
