"""Bounded mirror of shared consolidated memory into Illo Knowledge.

Knowledge search does not yet enforce per-user memory visibility.  This
connector therefore mirrors only ``org`` and ``team`` summaries; private
memory remains exclusively owned by the memory subsystem until the knowledge
index has an ACL-aware read path.  The mirror is derived and additive: it reads
``MemoryNode`` rows and never participates in memory recall or mutation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.kernel.config import KNOWLEDGE_CONNECTOR_BATCH_SIZE
from brain.platform.db.models.knowledge import KnowledgeItem
from brain.platform.db.models.reconstructive_memory import MemoryEdgeNode, MemoryNode
from brain.systems.knowledge.connectors.base import KnowledgeDraft

_SHARED_VISIBILITIES = ("org", "team")


def _cursor_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _draft_for_memory(
    node: MemoryNode,
    *,
    superseded_by: int | None,
) -> KnowledgeDraft:
    summary = str(node.text or node.canonical_label).strip()
    memory_kind = str(node.content_kind or node.node_kind).strip()
    scope = str(node.scope_key or "default").strip()
    superseded = node.truth_status == "superseded" or superseded_by is not None
    archived_at = node.archived_at or (node.updated_at if superseded else None)
    return KnowledgeDraft(
        source="memory",
        kind="memory",
        source_ref=f"memory_node:{node.id}",
        title=str(node.canonical_label).strip(),
        summary=summary,
        entities=list(dict.fromkeys((memory_kind, scope))),
        raw_text=summary,
        extra={
            "archived": node.archived_at is not None,
            "consolidated": True,
            "confidence": float(node.confidence or 0.0),
            "freshness_status": node.freshness_status,
            "memory_type": memory_kind,
            "node_kind": node.node_kind,
            "org_id": str(node.org_id) if node.org_id is not None else None,
            "scope": scope,
            "sensitivity": node.sensitivity,
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


def _withdrawn_draft(node: MemoryNode) -> KnowledgeDraft:
    """Scrub a formerly shared mirror after its source becomes private."""

    return KnowledgeDraft(
        source="memory",
        kind="memory",
        source_ref=f"memory_node:{node.id}",
        title="Memory no longer shared",
        summary="This consolidated memory is no longer shared with the workspace.",
        raw_text="",
        extra={
            "archived": True,
            "mirror_status": "visibility_withdrawn",
            "node_kind": node.node_kind,
            "truth_status": node.truth_status,
            "visibility": node.visibility,
        },
        source_created_at=node.created_at,
        source_updated_at=node.updated_at,
        archived_at=node.archived_at or node.updated_at,
    )


class MemoryConnector:
    """Enumerate shared consolidated memory nodes by update watermark."""

    source_key = "memory"

    def __init__(self, *, max_items: int = KNOWLEDGE_CONNECTOR_BATCH_SIZE):
        self.max_items = max(1, int(max_items))

    async def enumerate_changed(
        self,
        session: AsyncSession,
        cursor: dict[str, Any],
    ) -> tuple[list[KnowledgeDraft], dict[str, Any]]:
        marker = _cursor_datetime(cursor.get("updated_at"))
        marker_id = max(0, int(cursor.get("id") or 0))
        statement = (
            select(MemoryNode)
            .where(MemoryNode.node_kind == "summary")
            .order_by(MemoryNode.updated_at.asc(), MemoryNode.id.asc())
            .limit(self.max_items)
        )
        if marker is not None:
            statement = statement.where(
                or_(
                    MemoryNode.updated_at > marker,
                    and_(
                        MemoryNode.updated_at == marker,
                        MemoryNode.id > marker_id,
                    ),
                )
            )

        rows = list((await session.scalars(statement)).all())
        if not rows:
            return [], dict(cursor)
        source_refs = [f"memory_node:{node.id}" for node in rows]
        existing_refs = set(
            (
                await session.scalars(
                    select(KnowledgeItem.source_ref).where(
                        KnowledgeItem.source == self.source_key,
                        KnowledgeItem.source_ref.in_(source_refs),
                    )
                )
            ).all()
        )
        supersession_rows = (
            await session.execute(
                select(
                    MemoryEdgeNode.source_node_id,
                    MemoryEdgeNode.target_node_id,
                )
                .where(
                    MemoryEdgeNode.source_node_id.in_([node.id for node in rows])
                )
                .where(MemoryEdgeNode.edge_kind == "superseded_by")
                .order_by(MemoryEdgeNode.id.asc())
            )
        ).all()
        superseded_by = {
            source_node_id: target_node_id
            for source_node_id, target_node_id in supersession_rows
        }
        drafts = [
            _draft_for_memory(node, superseded_by=superseded_by.get(node.id))
            if node.visibility in _SHARED_VISIBILITIES
            else _withdrawn_draft(node)
            for node in rows
            if node.visibility in _SHARED_VISIBILITIES
            or f"memory_node:{node.id}" in existing_refs
        ]
        last = rows[-1]
        return drafts, {
            "updated_at": _utc_iso(last.updated_at),
            "id": last.id,
        }


__all__ = ["MemoryConnector"]
