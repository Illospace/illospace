"""Keep legacy recall calls on the same visible ledger data as the brain."""

import inspect
from dataclasses import asdict, is_dataclass

from illo.ledger import items, lanes, mcp
from illo.sources import search as shared_search

from .schemas import GetKnowledge, Search

# The search builder can add this function without a dependency on its service.
search = getattr(shared_search, "search", None)
SEARCH_USES_CONTEXT = bool(search and "ctx" in inspect.signature(search).parameters)
CAPABILITIES = ("knowledge.search", "knowledge.get", "workspace.search")


def hit(row, source: str, rank: int) -> dict:
    if is_dataclass(row):
        row = asdict(row)
        ref = row["source_ref"]
        identifier = ref.rsplit(":", 1)[-1].rsplit("#", 1)[-1]
        row.update(
            id=int(identifier) if identifier.isdigit() else ref,
            summary=row.get("snippet", ""),
            source_at=row.get("at"),
            scores={"lexical": row.get("lexical"), "recency": row.get("recency"),
                    "rrf": row.get("rrf", row["score"])},
        )
    at = row.get("source_at") or row.get("updated_at") or row.get("created_at")
    return {
        **row,
        "source": row.get("source", row.get("source_ref", source).split(":", 1)[0]),
        "source_ref": row.get("source_ref", f"lane:{row['id']}"),
        "kind": row.get("kind", "lane"),
        "summary": row.get("summary", ""),
        "source_created_at": row.get("created_at", at),
        "source_updated_at": at,
        "scores": row.get(
            "scores",
            {"lexical": float(row.get("score", 0)), "recency": None, "rrf": 1 / (60 + rank)},
        ),
    }


async def read(ctx, args: dict) -> dict:
    capability = args["capability"]
    if capability not in CAPABILITIES:
        raise ValueError("Unknown capability. Use: " + ", ".join(CAPABILITIES))
    if capability == "knowledge.get":
        return await get(ctx, GetKnowledge.model_validate(args["arguments"]))
    query = Search.model_validate(args["arguments"])
    limit = max(1, min(query.limit, 50))
    if search:
        if SEARCH_USES_CONTEXT:
            # Agent recall has no run in which to track a personal connector read.
            found = await search(
                ctx.app,
                ctx.principal,
                query.query,
                query.kinds or None,
                50,
                conn=ctx.conn,
                personal_allowed=False,
            )
        else:
            found = await search(ctx.principal, query.query, query.kinds or None, 50)
        rows = found["results"] if isinstance(found, dict) else found
        results = [hit(row, "items", i + 1) for i, row in enumerate(rows)]
    else:
        stored = await items.search(
            ctx.conn,
            query.query,
            principal=ctx.principal,
            kinds=query.kinds or None,
            limit=50,
        )
        results = [hit(row, "items", i + 1) for i, row in enumerate(stored)]
        if not query.kinds or "lane" in query.kinds:
            active = await lanes.search(ctx.conn, query.query, principal=ctx.principal, limit=50)
            results += [hit(row, "lanes", i + 1) for i, row in enumerate(active)]
        results.sort(key=lambda row: row["scores"]["rrf"], reverse=True)
    if query.sources:
        results = [row for row in results if row["source"] in query.sources]
    results = results[:limit]
    if capability == "workspace.search":
        return {"query": query.query, "results": results}
    return {
        "query": query.query,
        "sources": query.sources,
        "kinds": query.kinds,
        "semantic_available": False,
        "semantic_degraded_reason": "not_configured",
        "requested_limit": query.limit,
        "effective_limit": limit,
        "results": results,
    }


async def get(ctx, args: GetKnowledge) -> dict:
    ref = args.source_ref or args.ref or args.id
    if ref is None or not str(ref).strip():
        raise ValueError("Give source_ref, ref, or id")
    if isinstance(ref, int) or str(ref).isdigit():
        ref = await mcp.item_ref(ctx.conn, int(ref), principal=ctx.principal) or str(ref)
    row = await items.get(ctx.conn, str(ref), principal=ctx.principal)
    if not row and str(ref).startswith("lane:") and str(ref)[5:].isdigit():
        row = await lanes.get(ctx.conn, int(str(ref)[5:]), principal=ctx.principal)
        if row:
            row.update(source_ref=str(ref), body=row["summary"], kind="lane")
    if not row:
        # A hidden private item has the same answer as a missing item.
        return {"items": [], "missing": [str(ref)], "not_indexed": []}
    row["owner"] = row["owner_person_id"]
    return {**row, "items": [dict(row)], "missing": [], "not_indexed": []}
