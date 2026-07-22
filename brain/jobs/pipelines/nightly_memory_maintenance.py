#!/usr/bin/env python3
"""Auditable nightly expiry maintenance for reconstructive memory."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from time import perf_counter
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from brain.platform.db.models.reconstructive_memory import (
    MemoryAssertionNode,
    MemoryEdgeNode,
    MemoryNode,
)
from brain.platform.db.models.system import ConsolidationRun
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.reconstructive_memory.curation import archive_memory_by_policy

EXPIRY_GRACE_DAYS = 7
EXPIRY_RULE = "explicit_valid_until_at_least_7_days_past"
EXPIRY_POLICY_VERSION = "evidence-bound-expiry-v1"
PROTECTED_CONTENT_KINDS = frozenset({"policy", "procedure", "lesson"})
SUMMARY_CONTENT_KIND = "summary"


def _utcnow(now: datetime | None = None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _is_protected_kind(node: MemoryNode) -> bool:
    return str(node.content_kind or "").strip().lower() in PROTECTED_CONTENT_KINDS


async def _expired_content_nodes(
    session: AsyncSession,
    *,
    cutoff: datetime,
    org_id: str | None,
    lock: bool,
) -> list[MemoryNode]:
    statement = (
        select(MemoryNode)
        .where(MemoryNode.node_kind == "content")
        .where(MemoryNode.archived_at.is_(None))
        .where(MemoryNode.valid_until.isnot(None))
        .where(MemoryNode.valid_until <= cutoff)
        .order_by(MemoryNode.id)
    )
    if org_id is not None:
        statement = statement.where(MemoryNode.org_id == org_id)
    if lock:
        statement = statement.with_for_update(of=MemoryNode)
    return list((await session.scalars(statement)).all())


async def _active_current_summaries(
    session: AsyncSession,
    *,
    now: datetime,
    org_id: str | None,
) -> list[MemoryNode]:
    statement = (
        select(MemoryNode)
        .where(
            or_(
                MemoryNode.node_kind == SUMMARY_CONTENT_KIND,
                MemoryNode.content_kind == SUMMARY_CONTENT_KIND,
            )
        )
        .where(MemoryNode.archived_at.is_(None))
        .where(MemoryNode.truth_status != "superseded")
        .where(MemoryNode.freshness_status != "stale")
        .where(or_(MemoryNode.valid_until.is_(None), MemoryNode.valid_until > now))
        .order_by(MemoryNode.id)
    )
    if org_id is not None:
        statement = statement.where(MemoryNode.org_id == org_id)
    return list((await session.scalars(statement)).all())


async def _nodes_supporting_active_summaries(
    session: AsyncSession,
    *,
    candidates: list[MemoryNode],
    summaries: list[MemoryNode],
) -> set[int]:
    """Find support by direct graph dependency or shared assertion span lineage."""

    candidate_ids = {node.id for node in candidates}
    summary_ids = {node.id for node in summaries}
    if not candidate_ids or not summary_ids:
        return set()

    edge_rows = (
        await session.execute(
            select(MemoryEdgeNode.source_node_id, MemoryEdgeNode.target_node_id).where(
                or_(
                    and_(
                        MemoryEdgeNode.source_node_id.in_(candidate_ids),
                        MemoryEdgeNode.target_node_id.in_(summary_ids),
                    ),
                    and_(
                        MemoryEdgeNode.source_node_id.in_(summary_ids),
                        MemoryEdgeNode.target_node_id.in_(candidate_ids),
                    ),
                )
            )
        )
    ).all()
    supporting_ids = {
        source_id if source_id in candidate_ids else target_id
        for source_id, target_id in edge_rows
    }

    assertion_rows = list(
        (
            await session.scalars(
                select(MemoryAssertionNode).where(
                    MemoryAssertionNode.node_id.in_(candidate_ids | summary_ids)
                )
            )
        ).all()
    )
    summary_span_ids = {
        int(span_id)
        for assertion in assertion_rows
        if assertion.node_id in summary_ids
        for span_id in (assertion.source_span_ids or [])
        if span_id is not None
    }
    if summary_span_ids:
        supporting_ids.update(
            assertion.node_id
            for assertion in assertion_rows
            if assertion.node_id in candidate_ids
            and summary_span_ids.intersection(
                int(span_id)
                for span_id in (assertion.source_span_ids or [])
                if span_id is not None
            )
        )
    return supporting_ids


def _empty_report(*, apply: bool, cutoff: datetime) -> dict[str, Any]:
    return {
        "mode": "apply" if apply else "dry_run",
        "rule": EXPIRY_RULE,
        "policy_version": EXPIRY_POLICY_VERSION,
        "cutoff": cutoff.isoformat(),
        "candidates": 0,
        "candidate_node_ids": [],
        "eligible": 0,
        "eligible_node_ids": [],
        "archived": 0,
        "archived_node_ids": [],
        "excluded_by_rule": {
            "protected_kind": 0,
            "supports_active_current_summary": 0,
        },
        "errors": 0,
        "error_details": [],
        "duration_ms": 0.0,
    }


async def run_nightly_memory_maintenance(
    target_date: date,
    *,
    org_id: str | None = None,
    apply: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Report or atomically apply the conservative expiry policy."""

    started = perf_counter()
    maintenance_now = _utcnow(now)
    cutoff = maintenance_now - timedelta(days=EXPIRY_GRACE_DAYS)
    report = _empty_report(apply=apply, cutoff=cutoff)

    async with UnitOfWork() as uow:
        run = ConsolidationRun(
            run_date=target_date,
            phase="nightly_memory_maintenance",
            status="running",
            org_id=org_id,
            memories_created=0,
            edges_created=0,
            memories_decayed=0,
        )
        uow.session.add(run)
        await uow.session.flush()

        try:
            # A savepoint keeps the run ledger writable if candidate planning or
            # curation fails, while the outer unit of work remains one atomic
            # database transaction.
            async with uow.session.begin_nested():
                candidates = await _expired_content_nodes(
                    uow.session,
                    cutoff=cutoff,
                    org_id=org_id,
                    lock=apply,
                )
                summaries = await _active_current_summaries(
                    uow.session,
                    now=maintenance_now,
                    org_id=org_id,
                )
                summary_support_ids = await _nodes_supporting_active_summaries(
                    uow.session,
                    candidates=candidates,
                    summaries=summaries,
                )

                protected_ids = {node.id for node in candidates if _is_protected_kind(node)}
                summary_excluded_ids = summary_support_ids - protected_ids
                eligible_nodes = [
                    node
                    for node in candidates
                    if node.id not in protected_ids and node.id not in summary_support_ids
                ]
                report.update(
                    {
                        "candidates": len(candidates),
                        "candidate_node_ids": [node.id for node in candidates],
                        "eligible": len(eligible_nodes),
                        "eligible_node_ids": [node.id for node in eligible_nodes],
                        "excluded_by_rule": {
                            "protected_kind": len(protected_ids),
                            "supports_active_current_summary": len(summary_excluded_ids),
                        },
                    }
                )

                if apply:
                    for node in eligible_nodes:
                        reason = (
                            f"The node's explicit valid_until {node.valid_until.isoformat()} "
                            f"is on or before the seven-day grace cutoff {cutoff.isoformat()}, "
                            "and no protected kind or active current summary dependency applies."
                        )
                        await archive_memory_by_policy(
                            uow.session,
                            node=node,
                            rule=EXPIRY_RULE,
                            policy_version=EXPIRY_POLICY_VERSION,
                            run_id=run.id,
                            reason=reason,
                        )
                    report["archived"] = len(eligible_nodes)
                    report["archived_node_ids"] = [node.id for node in eligible_nodes]
        except Exception as exc:
            report["errors"] = 1
            report["error_details"] = [
                {
                    "type": type(exc).__name__,
                    "message": str(exc),
                }
            ]
            report["archived"] = 0
            report["archived_node_ids"] = []

        report["duration_ms"] = round((perf_counter() - started) * 1000, 3)
        report["run_id"] = run.id
        run.status = "failed" if report["errors"] else "completed"
        run.completed_at = maintenance_now
        run.memories_decayed = int(report["archived"])
        run.summary = json.dumps(report, sort_keys=True)
        await uow.session.flush()

    return report


async def _async_main(args: argparse.Namespace) -> dict[str, Any]:
    target = (
        datetime.strptime(args.date, "%Y-%m-%d").date()
        if args.date
        else (datetime.now(timezone.utc) - timedelta(days=1)).date()
    )
    return await run_nightly_memory_maintenance(
        target,
        org_id=args.org_id,
        apply=args.apply,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="Target date (YYYY-MM-DD), default yesterday")
    parser.add_argument("--org-id")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply",
        dest="apply",
        action="store_true",
        help="Soft-archive eligible nodes (default: report-only dry run)",
    )
    mode.add_argument(
        "--dry-run",
        dest="apply",
        action="store_false",
        help="Report eligible nodes without archiving (the default)",
    )
    parser.set_defaults(apply=False)
    args = parser.parse_args()
    report = asyncio.run(_async_main(args))
    print(json.dumps(report, sort_keys=True))
    if report["errors"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
