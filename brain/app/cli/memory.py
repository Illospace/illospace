#!/usr/bin/env python3
"""Illo Brain — memory operations.

CRUD for the memory graph. Uses UnitOfWork for DB access, embeddings.py for vectors.
CLI interface preserved for backward compatibility.
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import datetime

import brain.kernel.config as config
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.platform.db.repositories.memory_write_context import (
    MemoryWriteContext,
    dangerously_build_dev_test_memory_write_context,
)
from brain.platform.db.repositories.memory_visibility import MemoryVisibilityContext
from brain.systems.memory.attention_controller import AttentionController, observe_retrieval
from brain.systems.quality.gate import check_quality
from brain.systems.memory.embeddings import (
    embed_document,
    embed_query,
)


def _lazy_load_enabled(flag: bool | None = None) -> bool:
    if flag is not None:
        return flag
    return os.getenv("ATTENTION_LAZY_LOAD_ENABLED", "0").strip().lower() not in {"0", "false", "no"}


async def _expand_lazy_loaded_results(
    controller: AttentionController,
    *,
    attention_decision: dict,
    user_id: str | None,
    org_id: str | None,
    limit: int,
) -> list[dict]:
    retrieval_decision_id = attention_decision.get("retrieval_decision_id")
    if retrieval_decision_id is None:
        return []
    return await controller.load_lazy_candidates(
        retrieval_decision_id=int(retrieval_decision_id),
        user_id=user_id,
        org_id=org_id,
        limit=limit,
    )


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
    write_context: MemoryWriteContext | None = None,
    confidence: float | None = None,
    harvest_type: str | None = None,
    harvest_confidence: float | None = None,
    topic_tags: list[str] | None = None,
    memory_tier: str = "episodic",
) -> dict:
    """Add a memory node with embeddings and auto-edges."""
    # Quality gate — reject low-quality content before wasting compute
    qr = await check_quality(content, salience=salience, memory_type=memory_type)
    if not qr.passed:
        return {"rejected": True, "reason": qr.reason}
    if qr.adjusted_salience is not None:
        salience = qr.adjusted_salience

    # Auto-classify scope if not provided
    if scope is None:
        from brain.systems.memory.scope import classify_scope
        scope = classify_scope(content, memory_type)

    semantic_emb = embed_document(content)

    async with UnitOfWork() as uow:
        context = write_context
        if context is None:
            context = await dangerously_build_dev_test_memory_write_context(
                session=uow.session,
                source=source,
                source_session=source_session,
            )
        elif confidence is not None or source_session:
            context = context.with_defaults(
                source=source,
                session_id=source_session,
                confidence=confidence,
            )

        return await uow.memories.insert_memory(
            content=content,
            memory_type=memory_type,
            semantic_embedding=semantic_emb,
            salience=salience,
            tags=tags,
            related_ids=related_ids,
            rel_type=rel_type,
            decay_eligible=decay_eligible,
            scope=scope,
            memory_tier=memory_tier,
            harvest_type=harvest_type,
            harvest_confidence=harvest_confidence,
            topic_tags=topic_tags,
            context=context,
            auto_edge=True,
            auto_edge_k=config.AUTO_EDGE_K,
            auto_edge_threshold=0.5,
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
    """Query memories with multi-signal ranking.

    When use_pools=True, uses three-pool retrieval (exploit/explore/narrative)
    with adaptive bandit ratios. Falls back to existing behavior on failure.
    When use_pools=False (default), existing behavior is unchanged.
    """
    del emotion_context
    # Three-pool retrieval path
    if use_pools:
        try:
            return await _query_with_pools(
                query=query,
                limit=limit,
                org_id=org_id,
                user_id=user_id,
                attention_debug=attention_debug,
                expand_lazy_load=expand_lazy_load,
            )
        except Exception as e:
            import logging
            logging.getLogger(__name__).debug(
                "Pool retrieval failed, falling back to default: %s", e
            )
            # Fall through to existing behavior

    query_emb = embed_query(query)

    async with UnitOfWork() as uow:
        visibility_context = MemoryVisibilityContext(
            user_id=user_id,
            org_id=org_id,
            allow_global=(user_id == "system"),
        )
        retrieval = await uow.memories.query_ranked(
            query_embedding=query_emb,
            limit=limit,
            memory_type=memory_type,
            min_salience=min_salience,
            tags=tags,
            context=visibility_context,
            spread=spread,
        )
        results = retrieval["results"]
        spread_results = retrieval["spread_activation"]

        formatted_results = [{
            "id": r["id"], "content": r["content"], "type": r["memory_type"],
            "tier": r.get("memory_tier", "episodic"),
            "salience": r["salience"], "tags": r["tags"],
            "scores": {
                "combined": round(float(r["combined_score"] or 0), 4),
                "semantic": round(float(r["semantic_score"] or 0), 4),
                "recency": round(float(r["recency_score"] or 0), 4),
            },
            "created": r["created_at"].isoformat() if r["created_at"] else None,
        } for r in results]

    attention_decision = await observe_retrieval(
        stage="memory_query",
        query_text=query,
        candidates=formatted_results,
        user_id=user_id,
        org_id=org_id,
        preload_budget_tokens=limit * 120,
        lazy_budget_tokens=max(0, limit * 40),
    )
    selection = AttentionController().materialize_selection(formatted_results, attention_decision)
    lazy_loaded_results: list[dict] = []
    if _lazy_load_enabled(expand_lazy_load) and selection.lazy_load_eligible:
        lazy_loaded_results = await _expand_lazy_loaded_results(
            AttentionController(),
            attention_decision=attention_decision,
            user_id=user_id,
            org_id=org_id,
            limit=max(0, limit - len(selection.selected)),
        )
    visible_results = list(selection.selected) + list(lazy_loaded_results)
    result = {
        "query": query,
        "results": visible_results,
        "candidate_results": formatted_results,
        "suppressed_results": selection.suppressed,
        "lazy_load_results": selection.lazy_load_eligible,
        "lazy_loaded_results": lazy_loaded_results,
        "candidate_count": len(formatted_results),
        "count": len(visible_results),
        **({"spread_activation": [{
            "id": r["id"], "content": r["content"], "type": r["memory_type"],
            "via_relationship": r["relationship"],
            "edge_weight": round(r["edge_weight"], 3), "from_memory": r["from_id"],
        } for r in spread_results]} if spread_results else {}),
        "attention_decision": attention_decision,
    }
    if attention_debug:
        result["attention_explain"] = AttentionController().explain(attention_decision, formatted_results)
    return result


async def _query_with_pools(
    query: str,
    limit: int = 5,
    org_id: str | None = None,
    user_id: str | None = None,
    attention_debug: bool = False,
    expand_lazy_load: bool | None = None,
) -> dict:
    """Three-pool retrieval: exploit/explore/narrative with adaptive ratios.

    Uses PoolRetriever with adaptive bandit ratios from RetrievalPoolStatsRepository.
    Returns {"memories": [...], "retrieval_mode": "pools"}.
    """
    query_emb = embed_query(query)

    async with UnitOfWork() as uow:
        # Get adaptive ratios (falls back to defaults if no data)
        ratios = await uow.pool_stats.get_pool_ratios(org_id=org_id)
        results = await uow.memories.retrieve_with_pools(
            query_embedding=query_emb,
            limit=limit,
            org_id=org_id,
            user_id=user_id,
            ratios=ratios,
        )

    attention_decision = await observe_retrieval(
        stage="memory_query",
        query_text=query,
        candidates=results,
        user_id=user_id,
        org_id=org_id,
        preload_budget_tokens=limit * 120,
        lazy_budget_tokens=max(0, limit * 40),
    )
    selection = AttentionController().materialize_selection(results, attention_decision)
    lazy_loaded_results: list[dict] = []
    if _lazy_load_enabled(expand_lazy_load) and selection.lazy_load_eligible:
        lazy_loaded_results = await _expand_lazy_loaded_results(
            AttentionController(),
            attention_decision=attention_decision,
            user_id=user_id,
            org_id=org_id,
            limit=max(0, limit - len(selection.selected)),
        )
    visible_results = list(selection.selected) + list(lazy_loaded_results)
    result = {
        "memories": visible_results,
        "candidate_memories": results,
        "lazy_loaded_memories": lazy_loaded_results,
        "retrieval_mode": "pools",
        "query": query,
        "pool_ratios": ratios,
        "attention_decision": attention_decision,
    }
    if attention_debug:
        result["attention_explain"] = AttentionController().explain(attention_decision, results)
    return result


async def get_memory(memory_id: int) -> dict | None:
    """Get a single memory with its edges."""
    async with UnitOfWork() as uow:
        detail = await uow.memories.get_detail(memory_id)
        if not detail:
            return None
        memory = detail["memory"]
        edges = detail["edges"]

    result = {
        "id": memory.id,
        "content": memory.content,
        "memory_type": memory.memory_type,
        "salience": memory.salience,
        "source": memory.source,
        "tags": memory.tags,
        "created_at": memory.created_at,
        "last_accessed": memory.last_accessed,
        "access_count": memory.access_count,
        "decay_eligible": memory.decay_eligible,
        "archived": memory.archived,
    }
    result["edges"] = [{
        "connected_id": e["connected_id"], "relationship": e["relationship"],
        "weight": round(e["weight"], 3), "connected_content": e["connected_content"][:100],
        "connected_type": e["connected_type"], "auto": e["auto_generated"],
    } for e in edges]
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


def _write_context_from_args(args, *, source: str) -> MemoryWriteContext | None:
    user_id = getattr(args, "user_id", None)
    if not user_id:
        return None
    evidence = {}
    evidence_json = getattr(args, "evidence_json", None)
    if evidence_json:
        evidence = json.loads(evidence_json)
    org_id = getattr(args, "org_id", None)
    visibility = getattr(args, "visibility", None) or ("org" if org_id else "private")
    return MemoryWriteContext(
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
