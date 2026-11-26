"""Keep legacy recall calls on the same visible ledger data as the brain."""

import inspect
from dataclasses import asdict, is_dataclass
from pathlib import Path

from illo.ledger import items, lanes, mcp
from illo.sources import search as shared_search

from .schemas import GetKnowledge, Search

# The search builder can add this function without a dependency on its service.
search = getattr(shared_search, "search", None)
SEARCH_USES_CONTEXT = bool(search and "ctx" in inspect.signature(search).parameters)
CAPABILITIES = ("capabilities", "knowledge.search", "knowledge.get", "workspace.search")


def capabilities(ctx) -> dict:
    """Return a concise, secret-free self-map of what this Illo watches, knows, and can do."""
    from . import allowed_tools

    ws = ctx.app.workspace
    cfg = ws.config
    person_key = ctx.principal[7:] if ctx.principal.startswith("person:") else None
    channels = [
        {
            "id": ch.id,
            "name": ch.name,
            "mode": ch.mode,
            "visibility": ch.visibility,
        }
        for ch in cfg.slack.channels
        if ch.visibility == "team"
    ]
    rules = [
        {
            "name": r.name,
            "description": r.description,
            "channels": list(r.channels),
            "profiles": list(r.profiles),
        }
        for r in ws.rules
        if (not r.principals or "team" in r.principals or ctx.principal in r.principals)
        and r.principals != ["outsider"]
    ]
    task_lists = [
        {
            "key": entry.key,
            "name": entry.name,
            "description": entry.description,
            "holders": list(entry.holders),
            "default_holder": entry.default_holder,
            "done": entry.done,
        }
        for entry in ws.lists
        if not entry.owner or entry.owner == person_key
    ]
    newest_notes = sorted(ws.notes, key=lambda n: (n.created or "", n.path), reverse=True)
    notes = [
        {
            "ref": f"note:{Path(n.path).name}",
            "title": n.title,
            "about": n.about,
        }
        for n in newest_notes[:50]
    ]
    skills = [{"name": s.name, "description": s.description} for s in ws.skills]
    agents = [
        {
            "key": a.key,
            "name": a.name,
            "owner": a.owner,
            "kind": a.kind,
            "wake_type": a.wake.type,
        }
        for a in ws.agents
    ]
    tools = sorted(
        allowed_tools(
            ctx.token,
            pages_enabled=cfg.pages.enabled,
            board_enabled=cfg.board.enabled,
        )
    )
    return {
        "team_name": cfg.team_name,
        "default_owner": cfg.default_owner,
        "timezone": cfg.timezone,
        "watched": {
            "slack_channels": channels,
            "github_repositories": list(cfg.github.repositories),
            "github_task_list": cfg.github.task_list,
            "auto_assign_slack_bots_count": len(cfg.triage.auto_assign.slack_bots),
            "auto_assign_signal_sources": list(cfg.triage.auto_assign.signal_sources),
            "board_enabled": cfg.board.enabled,
            "board_coordinator_enabled": cfg.board.coordinator.enabled,
            "pages_enabled": cfg.pages.enabled,
        },
        "playbooks": {
            "rules": rules,
            "lists": task_lists,
            "notes": notes,
            "skills": skills,
        },
        "agents": agents,
        "how_illo_works": {
            "alerts_and_slack": (
                "Illo watches configured Slack channels (coordinator mode triages bot alerts and forms). "
                "Tainted alert runs see team rules (rules/) and team workflow lists (lists/), can search and file "
                "GitHub issues, auto-assign trusted triage.auto_assign.slack_bots to default_owner (others get an owner Assign card), "
                "and mirror issues into github.task_list when configured."
            ),
            "teaching_memory": (
                "A personal agent can teach Illo lasting team facts (note_save), workflow lists (list_save), "
                "or forget notes (note_forget) via illo_submit on its person's behalf. Changes pass Auto Mode safety review "
                "and send the person a Slack DM card with 1-click Correct/Forget undo. Team lists also apply to alert triage."
            ),
            "tasks_and_board": (
                "Use task_* tools for tracked team work and board_* tools for multi-agent threads. "
                "The board coordinator answers factual questions from team memory and routes open questions "
                "to the ticket requester and their connected personal agent before escalating to Slack."
            ),
            "asks_and_comms": (
                "Use ask_create (supports single owner or owners list) and ask_list for human decisions. "
                "Use illo_submit to ask Illo to post internal team Slack updates or teammate DMs (Auto Mode reviewed)."
            ),
        },
        "mcp_tools": tools,
    }


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
    if capability == "capabilities":
        return capabilities(ctx)
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
    if str(ref).startswith("slack-event:") or (row and row["kind"] == "slack"):
        canonical = row["source_ref"] if row else str(ref)
        row = await shared_search.get(ctx.app, ctx.principal, canonical, conn=ctx.conn)
    if not row and str(ref).startswith("lane:") and str(ref)[5:].isdigit():
        row = await lanes.get(ctx.conn, int(str(ref)[5:]), principal=ctx.principal)
        if row:
            row.update(source_ref=str(ref), body=row["summary"], kind="lane")
    if not row:
        # A hidden private item has the same answer as a missing item.
        return {"items": [], "missing": [str(ref)], "not_indexed": []}
    row["owner"] = row.get("owner_person_id")
    return {**row, "items": [dict(row)], "missing": [], "not_indexed": []}
