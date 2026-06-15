#!/usr/bin/env python3
"""Illo Brain — memory operations.

CRUD for the reconstructive memory graph.
CLI interface preserved for backward compatibility.
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime
from types import SimpleNamespace

from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.quality.gate import check_quality
from brain.systems.reconstructive_memory.controller import reconstruct_memory


# ============================================================
# Memory operations
# ============================================================

async def add_memory(
    content: str,
    memory_type: str,
    salience: float = 5.0,
    tags: list[str] | None = None,
    source: str = "conversation",
    related_ids: list[int] | None = None,
    scope: str | None = None,
    rel_type: str = "related_to",
    decay_eligible: bool = True,
    source_session: str | None = None,
    write_context: object | None = None,
    confidence: float | None = None,
    harvest_type: str | None = None,
    harvest_confidence: float | None = None,
    topic_tags: list[str] | None = None,
    memory_tier: str = "episodic",
) -> dict:
    """Add source-backed reconstructive memory."""
    qr = await check_quality(content, salience=salience, memory_type=memory_type)
    if not qr.passed:
        return {"rejected": True, "reason": qr.reason}
    if qr.adjusted_salience is not None:
        salience = qr.adjusted_salience

    del related_ids, rel_type, decay_eligible, scope, harvest_type, harvest_confidence, topic_tags, memory_tier
    async with UnitOfWork() as uow:
        context = _coerce_write_context(
            write_context,
            source=source,
            source_session=source_session,
            confidence=confidence if confidence is not None else max(0.0, min(1.0, salience / 10.0)),
        )
        return await uow.memories.insert_memory(
            content=content,
            memory_type=memory_type,
            salience=salience,
            tags=tags,
            context=context,
        )


async def query_memories(
    query: str,
    limit: int = 5,
    memory_type: str | None = None,
    min_salience: float | None = None,
    tags: list[str] | None = None,
    spread: bool = False,
    use_pools: bool = False,
    user_id: str | None = None,
    org_id: str | None = None,
    emotion_context: str | None = None,
    attention_debug: bool = False,
    expand_lazy_load: bool | None = None,
) -> dict:
    """Query memory by reconstructing a source-backed evidence pack."""
    del memory_type, min_salience, tags, spread, use_pools, emotion_context, attention_debug, expand_lazy_load
    async with UnitOfWork() as uow:
        pack = await reconstruct_memory(
            uow.session,
            query=query,
            limit=limit,
            org_id=org_id,
            user_id=user_id,
        )
    evidence_pack = pack.to_dict()
    results = [
        {
            "id": item["node_id"],
            "content": item.get("source_text") or item.get("text") or "",
            "type": "reconstructed_evidence",
            "tier": "source_backed",
            "salience": round(float(item.get("confidence") or 0.0) * 10, 2),
            "scores": {"confidence": round(float(item.get("confidence") or 0.0), 4)},
            "source_span_id": item.get("source_span_id"),
            "assertion_id": item.get("assertion_id"),
        }
        for item in evidence_pack["supporting_evidence"]
    ]
    return {
        "query": query,
        "results": results,
        "candidate_results": results,
        "suppressed_results": [],
        "lazy_load_results": [],
        "lazy_loaded_results": [],
        "candidate_count": len(results),
        "count": len(results),
        "retrieval_mode": "reconstructive",
        "evidence_pack": evidence_pack,
    }


async def _query_with_pools(
    query: str,
    limit: int = 5,
    org_id: str | None = None,
    user_id: str | None = None,
    attention_debug: bool = False,
    expand_lazy_load: bool | None = None,
) -> dict:
    del attention_debug, expand_lazy_load
    return await query_memories(query=query, limit=limit, org_id=org_id, user_id=user_id)


async def get_memory(memory_id: int) -> dict | None:
    """Get a single memory with its edges."""
    async with UnitOfWork() as uow:
        detail = await uow.memories.get_detail(memory_id)
        if not detail:
            return None
        memory = detail["memory"]
        edges = detail["edges"]

    result = dict(vars(memory))
    result["edges"] = edges
    return result


async def get_context(memory_id: int, depth: int = 2) -> dict:
    """Get memory with full graph neighborhood (spreading activation)."""
    async with UnitOfWork() as uow:
        return await uow.memories.get_graph_context(memory_id, depth=depth)


async def list_memories(
    memory_type: str | None = None, limit: int = 20,
    min_salience: float | None = None, tags: list[str] | None = None,
) -> list[dict]:
    """List memories with optional filters."""
    async with UnitOfWork() as uow:
        memories = await uow.memories.list_filtered(
            memory_type=memory_type,
            limit=limit,
            min_salience=min_salience,
            tags=tags,
        )
        return [
            {
                "id": memory.id,
                "content": memory.content,
                "memory_type": memory.memory_type,
                "salience": memory.salience,
                "tags": memory.tags,
                "created_at": memory.created_at,
            }
            for memory in memories
        ]


async def get_stats() -> dict:
    """Database statistics."""
    async with UnitOfWork() as uow:
        return await uow.memories.stats()


async def connect_memories(source: int, target: int, rel: str, weight: float = 1.0) -> int:
    """Create an edge between two memories."""
    async with UnitOfWork() as uow:
        return await uow.edges.upsert_edge(
            source,
            target,
            rel,
            weight=weight,
            auto_generated=False,
        )


# ============================================================
# CLI (backward-compatible wrappers)
# ============================================================

async def cmd_add(args):
    related = [int(x.strip()) for x in args.related.split(",")] if args.related else None
    tags = [t.strip() for t in args.tags.split(",")] if args.tags else None
    write_context = _write_context_from_args(args, source=args.source or "conversation")
    result = await add_memory(
        content=args.content, memory_type=args.type, salience=args.salience,
        tags=tags, source=args.source or "conversation",
        related_ids=related, rel_type=args.rel_type or "related_to",
        decay_eligible=not args.no_decay,
        write_context=write_context,
    )
    print(json.dumps(result, indent=2))


async def cmd_query(args):
    tags = [t.strip() for t in args.tags.split(",")] if args.tags else None
    result = await query_memories(
        query=args.query, limit=args.limit, memory_type=args.type,
        min_salience=args.min_salience, tags=tags,
        spread=args.spread,
        attention_debug=args.attention_debug,
        expand_lazy_load=args.expand_lazy_load,
        user_id=args.user_id,
        org_id=args.org_id,
    )
    print(json.dumps(result, indent=2, default=str))


async def cmd_get(args):
    result = await get_memory(args.id)
    print(json.dumps(result or {"error": f"Memory {args.id} not found"}, indent=2, default=str))


async def cmd_context(args):
    result = await get_context(args.id, depth=args.depth)
    print(json.dumps(result, indent=2, default=str))


async def cmd_connect(args):
    eid = await connect_memories(args.source, args.target, args.rel, args.weight)
    print(json.dumps({"edge_id": eid, "source": args.source, "target": args.target, "rel": args.rel}))


async def cmd_list(args):
    tags = [t.strip() for t in args.tags.split(",")] if args.tags else None
    results = await list_memories(memory_type=args.type, limit=args.limit, min_salience=args.min_salience, tags=tags)
    print(json.dumps([{
        "id": r["id"], "content": r["content"][:200], "type": r["memory_type"],
        "salience": r["salience"], "tags": r["tags"],
        "created": r["created_at"].isoformat() if r["created_at"] else None,
    } for r in results], indent=2, default=str))


async def cmd_stats(args):
    print(json.dumps(await get_stats(), indent=2, default=str))


async def cmd_decay(args):
    async with UnitOfWork() as uow:
        candidates = await uow.memories.list_decay_candidates(
            days=args.days,
            threshold=args.threshold,
        )

        if args.dry_run:
            print(json.dumps({"dry_run": True, "would_archive": len(candidates),
                "candidates": [{"id": c.id, "content": c.content[:100], "salience": c.salience} for c in candidates]
            }, indent=2, default=str))
            return

        ids = [c.id for c in candidates]
        await uow.memories.archive_many(ids)
        print(json.dumps({"archived": len(ids), "ids": ids}))


async def cmd_index(args):
    async with UnitOfWork() as uow:
        memories = await uow.memories.list_index_memories(limit=args.limit)

    by_type: dict = {}
    for m in memories:
        by_type.setdefault(m.memory_type, []).append({
            "id": m.id, "content": m.content[:150],
            "salience": m.salience,
        })
    print(json.dumps({
        "generated": datetime.now().isoformat(), "total_active": len(memories),
        "memories_by_type": by_type,
    }, indent=2, default=str))


def _write_context_from_args(args, *, source: str) -> SimpleNamespace | None:
    user_id = getattr(args, "user_id", None)
    if not user_id:
        return None
    evidence = {}
    evidence_json = getattr(args, "evidence_json", None)
    if evidence_json:
        evidence = json.loads(evidence_json)
    org_id = getattr(args, "org_id", None)
    visibility = getattr(args, "visibility", None) or ("org" if org_id else "private")
    return SimpleNamespace(
        user_id=user_id,
        org_id=org_id,
        visibility=visibility,
        source=source,
        conversation_id=getattr(args, "conversation_id", None),
        idea_id=getattr(args, "idea_id", None),
        run_id=getattr(args, "run_id", None),
        session_id=getattr(args, "session_id", None),
        confidence=getattr(args, "confidence", None),
        evidence=evidence,
        source_ref=lambda: (
            f"conversation:{getattr(args, 'conversation_id', None)}"
            if getattr(args, "conversation_id", None)
            else None
        ),
        source_session=lambda: getattr(args, "session_id", None),
    )


def _coerce_write_context(
    context: object | None,
    *,
    source: str,
    source_session: str | None,
    confidence: float,
) -> object:
    if context is not None:
        if hasattr(context, "with_defaults") and (confidence is not None or source_session):
            return context.with_defaults(source=source, session_id=source_session, confidence=confidence)
        return context
    return SimpleNamespace(
        user_id=None,
        org_id=None,
        visibility="private",
        source=source,
        confidence=confidence,
        source_ref=lambda: None,
        source_session=lambda: source_session,
    )


async def cmd_import_md(args):
    import re
    if not os.path.exists(args.file):
        print(json.dumps({"error": f"File not found: {args.file}"}))
        return

    with open(args.file) as f:
        content = f.read()
    filename = os.path.basename(args.file)
    sections = re.split(r'^## ', content, flags=re.MULTILINE)

    imported = []
    for section in sections[1:]:
        lines = section.strip().split("\n")
        title = lines[0].strip()
        body = "\n".join(lines[1:]).strip()
        if not body or len(body) < 20:
            continue

        mtype = "fact"
        sal = 5.0
        if "lesson" in filename.lower() or "lesson" in title.lower():
            mtype, sal = "lesson", 8.0
        elif "decision" in title.lower():
            mtype, sal = "decision", 6.0
        elif "bug" in title.lower() or "fix" in title.lower():
            mtype, sal = "episode", 7.0

        dense = f"[{title}] {body[:500]}"
        result = await add_memory(content=dense, memory_type=mtype, salience=sal,
                                  source=f"import:{filename}", tags=[filename.replace(".md", "")])
        imported.append({"id": result["id"], "title": title, "type": mtype})

    print(json.dumps({"imported": len(imported), "memories": imported}, indent=2))


# ============================================================
# Argument parser
# ============================================================

async def main():
    parser = argparse.ArgumentParser(description="Illo Memory System")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("add")
    p.add_argument("--content", "-c", required=True)
    p.add_argument("--type", "-t", required=True, choices=["episode", "fact", "lesson", "decision", "preference", "procedure", "pattern", "insight", "observation"])
    p.add_argument("--salience", "-s", type=float, default=5.0)
    p.add_argument("--tags")
    p.add_argument("--related"); p.add_argument("--rel-type", default="related_to")
    p.add_argument("--source", default="conversation"); p.add_argument("--no-decay", action="store_true")
    p.add_argument("--user-id")
    p.add_argument("--org-id")
    p.add_argument("--visibility", choices=["private", "team", "org"], default=None)
    p.add_argument("--conversation-id")
    p.add_argument("--idea-id")
    p.add_argument("--run-id")
    p.add_argument("--session-id")
    p.add_argument("--confidence", type=float)
    p.add_argument("--evidence-json")

    p = sub.add_parser("query")
    p.add_argument("query"); p.add_argument("--limit", "-l", type=int, default=5)
    p.add_argument("--type", "-t"); p.add_argument("--min-salience", type=float)
    p.add_argument("--tags")
    p.add_argument("--spread", action="store_true")
    p.add_argument("--attention-debug", action="store_true")
    p.add_argument("--expand-lazy-load", action="store_true")
    p.add_argument("--user-id")
    p.add_argument("--org-id")

    p = sub.add_parser("get"); p.add_argument("id", type=int)
    p = sub.add_parser("context"); p.add_argument("id", type=int); p.add_argument("--depth", "-d", type=int, default=2)

    p = sub.add_parser("connect")
    p.add_argument("--source", type=int, required=True); p.add_argument("--target", type=int, required=True)
    p.add_argument("--rel", required=True); p.add_argument("--weight", type=float, default=1.0)

    p = sub.add_parser("list"); p.add_argument("--type", "-t"); p.add_argument("--limit", "-l", type=int, default=20)
    p.add_argument("--min-salience", type=float); p.add_argument("--tags")

    sub.add_parser("stats")

    p = sub.add_parser("decay"); p.add_argument("--days", type=int, default=30)
    p.add_argument("--threshold", type=float, default=2.0); p.add_argument("--dry-run", action="store_true")

    p = sub.add_parser("index"); p.add_argument("--limit", "-l", type=int, default=50)

    p = sub.add_parser("import-md"); p.add_argument("file")

    args = parser.parse_args()
    cmd_map = {
        "add": cmd_add, "query": cmd_query, "get": cmd_get, "context": cmd_context,
        "connect": cmd_connect, "list": cmd_list, "stats": cmd_stats, "decay": cmd_decay,
        "index": cmd_index, "import-md": cmd_import_md,
    }
    await cmd_map[args.command](args)


if __name__ == "__main__":
    asyncio.run(main())
