#!/usr/bin/env python3
"""Archive legacy post-run auto-encoded memory exhaust.

The default mode is a dry run. Pass ``--apply`` to soft-archive matching
content nodes and cue/tag nodes that are not shared with active durable
content. Sources, spans, assertions, embeddings, and graph edges are retained
for provenance. Apply mode locks the selected nodes for the transaction and
must run while memory writers are paused.

Usage:
    venv/bin/python scripts/archive_auto_encoded_memories.py
    venv/bin/python scripts/archive_auto_encoded_memories.py \
        --apply --confirm-writers-paused
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.reconstructive_memory import MemoryEdgeNode, MemoryNode
from brain.platform.db.repositories.reconstructive_memory import (
    ReconstructiveMemoryCompatibilityRepository,
)
from brain.platform.db.repositories.unit_of_work import UnitOfWork

AUTO_ENCODED_PREFIX = "[auto-encoded]"
CONTENT_BEARING_NODE_KINDS = ("content", "summary", "procedure", "policy")


@dataclass(frozen=True)
class ArchivePlan:
    """Active memory nodes selected for one atomic archive operation."""

    content_node_ids: tuple[int, ...] = ()
    orphaned_cue_node_ids: tuple[int, ...] = ()
    orphaned_tag_node_ids: tuple[int, ...] = ()

    @property
    def node_ids(self) -> tuple[int, ...]:
        return (
            *self.content_node_ids,
            *self.orphaned_cue_node_ids,
            *self.orphaned_tag_node_ids,
        )

    def report(self, *, applied: bool) -> dict[str, int | bool]:
        total = len(self.node_ids)
        return {
            "applied": applied,
            "archived": total if applied else 0,
            "would_archive": 0 if applied else total,
            "content_nodes": len(self.content_node_ids),
            "orphaned_cue_nodes": len(self.orphaned_cue_node_ids),
            "orphaned_tag_nodes": len(self.orphaned_tag_node_ids),
        }


async def _active_auto_encoded_content_ids(
    session: AsyncSession,
    *,
    lock: bool,
) -> tuple[int, ...]:
    statement = (
        select(MemoryNode.id)
        .where(MemoryNode.node_kind == "content")
        .where(MemoryNode.archived_at.is_(None))
        .where(MemoryNode.text.startswith(AUTO_ENCODED_PREFIX, autoescape=True))
        .order_by(MemoryNode.id)
    )
    if lock:
        statement = statement.with_for_update(of=MemoryNode)
    ids = await session.scalars(statement)
    return tuple(int(node_id) for node_id in ids)


async def _active_route_node_ids(
    session: AsyncSession,
    *,
    content_node_ids: tuple[int, ...],
    node_kind: str,
    lock: bool,
) -> tuple[int, ...]:
    if node_kind == "tag":
        route_join = MemoryEdgeNode.source_node_id == MemoryNode.id
        content_edge = MemoryEdgeNode.target_node_id.in_(content_node_ids)
        edge_kind = "tag_to_content"
    elif node_kind == "cue":
        route_join = MemoryEdgeNode.target_node_id == MemoryNode.id
        content_edge = MemoryEdgeNode.source_node_id.in_(content_node_ids)
        edge_kind = "content_to_cue"
    else:  # pragma: no cover - private caller controls the supported kinds
        raise ValueError(f"Unsupported route node kind: {node_kind}")

    statement = (
        select(MemoryNode.id)
        .join(MemoryEdgeNode, route_join)
        .where(MemoryNode.node_kind == node_kind)
        .where(MemoryNode.archived_at.is_(None))
        .where(MemoryEdgeNode.edge_kind == edge_kind)
        .where(content_edge)
        .order_by(MemoryNode.id)
    )
    if lock:
        statement = statement.with_for_update(of=MemoryNode)
    ids = await session.scalars(statement)
    return tuple(sorted({int(node_id) for node_id in ids}))


async def _route_nodes_shared_with_active_content(
    session: AsyncSession,
    *,
    route_node_ids: tuple[int, ...],
    archived_content_ids: tuple[int, ...],
    node_kind: str,
) -> set[int]:
    if not route_node_ids:
        return set()

    if node_kind == "tag":
        route_id = MemoryEdgeNode.source_node_id
        content_join = MemoryEdgeNode.target_node_id == MemoryNode.id
        edge_kind = "tag_to_content"
    elif node_kind == "cue":
        route_id = MemoryEdgeNode.target_node_id
        content_join = MemoryEdgeNode.source_node_id == MemoryNode.id
        edge_kind = "content_to_cue"
    else:  # pragma: no cover - private caller controls the supported kinds
        raise ValueError(f"Unsupported route node kind: {node_kind}")

    ids = await session.scalars(
        select(route_id)
        .join(MemoryNode, content_join)
        .where(route_id.in_(route_node_ids))
        .where(MemoryEdgeNode.edge_kind == edge_kind)
        .where(MemoryNode.node_kind.in_(CONTENT_BEARING_NODE_KINDS))
        .where(MemoryNode.archived_at.is_(None))
        .where(~MemoryNode.id.in_(archived_content_ids))
        .distinct()
    )
    return {int(node_id) for node_id in ids}


async def build_archive_plan(session: AsyncSession, *, lock: bool = False) -> ArchivePlan:
    """Select active exhaust and route nodes made orphaned by its archive."""

    content_node_ids = await _active_auto_encoded_content_ids(session, lock=lock)
    if not content_node_ids:
        return ArchivePlan()

    cue_node_ids = await _active_route_node_ids(
        session,
        content_node_ids=content_node_ids,
        node_kind="cue",
        lock=lock,
    )
    tag_node_ids = await _active_route_node_ids(
        session,
        content_node_ids=content_node_ids,
        node_kind="tag",
        lock=lock,
    )
    shared_cue_ids = await _route_nodes_shared_with_active_content(
        session,
        route_node_ids=cue_node_ids,
        archived_content_ids=content_node_ids,
        node_kind="cue",
    )
    shared_tag_ids = await _route_nodes_shared_with_active_content(
        session,
        route_node_ids=tag_node_ids,
        archived_content_ids=content_node_ids,
        node_kind="tag",
    )

    return ArchivePlan(
        content_node_ids=content_node_ids,
        orphaned_cue_node_ids=tuple(
            node_id for node_id in cue_node_ids if node_id not in shared_cue_ids
        ),
        orphaned_tag_node_ids=tuple(
            node_id for node_id in tag_node_ids if node_id not in shared_tag_ids
        ),
    )


async def archive_auto_encoded_memories(
    session: AsyncSession,
    *,
    apply: bool = False,
) -> ArchivePlan:
    """Build the cleanup plan and optionally archive it through ``archive_many``."""

    plan = await build_archive_plan(session, lock=apply)
    if apply:
        await ReconstructiveMemoryCompatibilityRepository(session).archive_many(plan.node_ids)
    return plan


async def _run(*, apply: bool) -> ArchivePlan:
    async with UnitOfWork() as uow:
        return await archive_auto_encoded_memories(uow.session, apply=apply)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Archive the selected nodes (default: dry run; requires paused writers)",
    )
    parser.add_argument(
        "--confirm-writers-paused",
        action="store_true",
        help="Confirm memory-writing workers are paused for the apply transaction",
    )
    args = parser.parse_args()
    if args.apply and not args.confirm_writers_paused:
        parser.error("--apply requires --confirm-writers-paused")
    plan = asyncio.run(_run(apply=args.apply))
    print(json.dumps(plan.report(applied=args.apply), sort_keys=True))


if __name__ == "__main__":
    main()
