"""Memory index eligibility shared by ingestion and knowledge reads."""

from __future__ import annotations

from collections.abc import Sequence
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.reconstructive_memory import MemoryEdgeNode, MemoryNode

SHARED_VISIBILITIES = ("org", "team")
KNOWLEDGE_NODE_KINDS = ("content",)


class MemoryIndexExclusionReason(StrEnum):
    PRIVATE_VISIBILITY = "private_visibility"
    NOT_A_CONTENT_NODE = "not_a_content_node"
    ARCHIVED_OR_SUPERSEDED = "archived_or_superseded"
    NOT_YET_INDEXED = "not_yet_indexed"


def is_superseded(node: Any, superseded_by: int | None) -> bool:
    return node.truth_status == "superseded" or superseded_by is not None


def memory_index_exclusion_reason(
    node: Any,
    *,
    superseded_by: int | None,
    mirror_archived: bool = False,
    has_mirror: bool = True,
) -> MemoryIndexExclusionReason | None:
    """Classify index exclusion, with node-level causes before mirror state.

    The default mirror state checks only whether a node can be mirrored live.
    ``node`` may be a MemoryNode or a projection of its eligibility fields.
    """

    if node.archived_at is not None or is_superseded(node, superseded_by):
        return MemoryIndexExclusionReason.ARCHIVED_OR_SUPERSEDED
    if node.node_kind not in KNOWLEDGE_NODE_KINDS:
        return MemoryIndexExclusionReason.NOT_A_CONTENT_NODE
    if node.visibility not in SHARED_VISIBILITIES:
        return MemoryIndexExclusionReason.PRIVATE_VISIBILITY
    if mirror_archived:
        return MemoryIndexExclusionReason.ARCHIVED_OR_SUPERSEDED
    if not has_mirror:
        return MemoryIndexExclusionReason.NOT_YET_INDEXED
    return None


async def load_superseded_by(
    session: AsyncSession, node_ids: Sequence[int]
) -> dict[int, int]:
    rows = (await session.execute(
        select(MemoryEdgeNode.source_node_id, MemoryEdgeNode.target_node_id)
        .where(MemoryEdgeNode.source_node_id.in_(node_ids))
        .where(MemoryEdgeNode.edge_kind == "superseded_by")
        .order_by(MemoryEdgeNode.id.asc())
    )).all()
    return dict(rows)


async def memory_node_index_exclusion_reason(
    session: AsyncSession, node: MemoryNode,
) -> MemoryIndexExclusionReason | None:
    """Check a stored node's eligibility without assuming its mirror exists yet."""
    superseded_by = await load_superseded_by(session, [node.id])
    return memory_index_exclusion_reason(node, superseded_by=superseded_by.get(node.id))


__all__ = [
    "KNOWLEDGE_NODE_KINDS",
    "SHARED_VISIBILITIES",
    "MemoryIndexExclusionReason",
    "is_superseded",
    "load_superseded_by",
    "memory_index_exclusion_reason",
    "memory_node_index_exclusion_reason",
]
