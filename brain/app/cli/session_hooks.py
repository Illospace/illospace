#!/usr/bin/env python3
"""Session hooks backed by reconstructive memory."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta

from sqlalchemy import func, select, text

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))

from brain.app.cli.memory import add_memory
from brain.platform.db.models.reconstructive_memory import MemoryEdgeNode, MemoryNode
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.memory.retrieval_feedback import apply_retrieval_feedback
from brain.systems.reconstructive_memory.controller import reconstruct_memory


def _memory_scope_from_env() -> dict[str, str | bool | None]:
    return {
        "user_id": os.environ.get("ILLO_USER_ID"),
        "org_id": os.environ.get("ILLO_ORG_ID"),
        "allow_global": os.environ.get("ILLO_ALLOW_GLOBAL_MEMORY") == "1",
    }


async def get_cross_channel_context(
    current_session: str | None = None,
    hours: int = 24,
    limit: int = 20,
    *,
    user_id: str | None = None,
    org_id: str | None = None,
    allow_global: bool = False,
) -> list[dict]:
    del current_session, allow_global
    cutoff = datetime.now() - timedelta(hours=hours)
    async with UnitOfWork() as uow:
        stmt = (
            select(MemoryNode)
            .where(MemoryNode.archived_at.is_(None))
            .where(MemoryNode.created_at >= cutoff)
            .order_by(MemoryNode.confidence.desc(), MemoryNode.created_at.desc())
            .limit(limit)
        )
        if org_id:
            stmt = stmt.where(MemoryNode.org_id == org_id)
        elif user_id:
            stmt = stmt.where(MemoryNode.user_id == user_id)
        rows = (await uow.session.scalars(stmt)).all()
    return [
        {
            "id": row.id,
            "content": (row.text or row.canonical_label or "")[:300],
            "type": row.content_kind or row.node_kind,
            "memory_type": row.content_kind or row.node_kind,
            "salience": round(float(row.confidence or 0.0) * 10, 2),
            "source": "reconstructive_memory",
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "tags": [row.node_kind],
        }
        for row in rows
    ]


async def _sense_context(message: str, timeout_s: int = 30) -> list[dict]:
    del timeout_s
    scope = _memory_scope_from_env()
    async with UnitOfWork() as uow:
        pack = await reconstruct_memory(
            uow.session,
            query=message,
            user_id=scope["user_id"],
            org_id=scope["org_id"],
            limit=5,
        )
        context = [
            {
                "id": item.node_id,
                "content": (item.source_text or item.text)[:200],
                "type": "reconstructed_evidence",
                "salience": round(float(item.confidence) * 10, 2),
                "similarity": round(float(item.confidence), 3),
                "source_span_id": item.source_span_id,
            }
            for item in pack.supporting_evidence
        ]
        await uow.session.execute(
            text("""
                INSERT INTO retrieval_log (query_text, results_returned, top_result_id, top_score)
                VALUES (:query_text, :results_returned, :top_result_id, :top_score)
            """),
            {
                "query_text": message[:200],
                "results_returned": len(context),
                "top_result_id": context[0]["id"] if context else None,
                "top_score": context[0]["similarity"] if context else None,
            },
        )
    return context


async def cmd_wake(args):
    scope = _memory_scope_from_env()
    cross_channel = await get_cross_channel_context(
        current_session=args.session,
        user_id=scope["user_id"],
        org_id=scope["org_id"],
        allow_global=bool(scope["allow_global"]),
    )
    print(json.dumps({"cross_channel_context": cross_channel}, indent=2, default=str))


async def cmd_sense(args):
    context = await _sense_context(args.message, timeout_s=getattr(args, "timeout", 30) or 30)
    print(json.dumps({"relevant_context": context, "context_count": len(context)}, indent=2, default=str))


async def cmd_sense_context(args):
    await cmd_sense(args)


async def cmd_encode(args):
    tags = [t.strip() for t in args.tags.split(",")] if args.tags else None
    result = await add_memory(
        content=args.content,
        memory_type=args.type or "episode",
        salience=args.salience or 5.0,
        tags=tags,
        source="session",
        source_session=getattr(args, "source_session", None),
    )
    print(json.dumps(result, indent=2))


async def cmd_log_retrieval(args):
    async with UnitOfWork() as uow:
        row = (await uow.session.execute(text("SELECT id FROM retrieval_log ORDER BY timestamp DESC LIMIT 1"))).mappings().first()
    if not row:
        print(json.dumps({"logged": False, "error": "no retrieval_log entries"}))
        return
    result = await apply_retrieval_feedback(row["id"], args.feedback)
    print(json.dumps({"logged": True, **result}))


async def cmd_sleep(args):
    if args.summary:
        await add_memory(
            content=args.summary,
            memory_type="episode",
            salience=5.0,
            tags=[datetime.now().strftime("%Y-%m-%d")],
            source="session-end",
        )
    async with UnitOfWork() as uow:
        ret = (await uow.session.execute(text("""
            SELECT COUNT(*) as total,
                   COUNT(*) FILTER (WHERE feedback = 'hit') as hits,
                   COUNT(*) FILTER (WHERE feedback = 'miss') as misses
            FROM retrieval_log WHERE timestamp::date = CURRENT_DATE
        """))).mappings().first()
    print(json.dumps({
        "session_encoded": bool(args.summary),
        "retrievals_today": ret["total"],
        "retrieval_hits": ret["hits"],
        "retrieval_misses": ret["misses"],
    }))


async def cmd_status(args):
    del args
    async with UnitOfWork() as uow:
        mem_count = await uow.session.scalar(select(func.count(MemoryNode.id)).where(MemoryNode.archived_at.is_(None))) or 0
        edge_count = await uow.session.scalar(select(func.count(MemoryEdgeNode.id))) or 0
        skill_rows = (await uow.session.execute(text(
            "SELECT maturity, COUNT(*) as cnt FROM skills WHERE NOT archived GROUP BY maturity"
        ))).mappings().all()
    print(json.dumps({
        "brain_health": {"memory_nodes": int(mem_count), "edges": int(edge_count)},
        "skill_maturity": {r["maturity"]: r["cnt"] for r in skill_rows},
    }, indent=2, default=str))


async def main():
    parser = argparse.ArgumentParser(description="Illo reconstructive memory session hooks")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("wake"); p.add_argument("--session", default=None)
    p = sub.add_parser("sense"); p.add_argument("message"); p.add_argument("--timeout", type=int, default=30)
    p = sub.add_parser("sense-context"); p.add_argument("message"); p.add_argument("--timeout", type=int, default=60)
    p = sub.add_parser("encode"); p.add_argument("content")
    p.add_argument("--type", default="episode"); p.add_argument("--salience", type=float, default=5.0)
    p.add_argument("--tags"); p.add_argument("--source-session")
    p = sub.add_parser("log-retrieval"); p.add_argument("--feedback", required=True, choices=["hit", "miss", "partial"])
    p = sub.add_parser("sleep"); p.add_argument("summary", nargs="?")
    sub.add_parser("status")
    args = parser.parse_args()
    commands = {
        "wake": cmd_wake,
        "sense": cmd_sense,
        "sense-context": cmd_sense_context,
        "encode": cmd_encode,
        "log-retrieval": cmd_log_retrieval,
        "sleep": cmd_sleep,
        "status": cmd_status,
    }
    await commands[args.command](args)


if __name__ == "__main__":
    asyncio.run(main())
