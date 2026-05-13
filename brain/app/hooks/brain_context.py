#!/usr/bin/env python3
"""
Brain Context — Returns relevant memories and guardrails for an incoming message.

Called by optional session integrations when a new message needs memory context.
Output is injected as a system message so the agent cannot ignore it.

Usage:
    python3 brain_context.py "user message text"

Output: JSON with relevant_memories, guardrails, and warnings.
"""

import json
import os
import sys
import inspect
from typing import Any

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))

from brain.platform.async_bridge import run_async_from_sync


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _session_execute(session: Any, *args: Any, **kwargs: Any) -> Any:
    return await _maybe_await(session.execute(*args, **kwargs))


async def async_get_context(
    message: str,
    *,
    user_id: str | None = None,
    org_id: str | None = None,
) -> dict:
    """Query the brain for context relevant to this message."""
    from sqlalchemy import text
    from brain.platform.db.repositories.unit_of_work import UnitOfWork
    from brain.systems.memory.embeddings import embed_query

    user_id = user_id or os.environ.get("BRAIN_USER_ID")
    org_id = org_id or os.environ.get("BRAIN_ORG_ID")

    result = {
        "memories": [],
        "guardrails": [],
        "warnings": [],
    }

    # 1. Semantic search for relevant memories
    try:
        query_emb = embed_query(message)
        emb_str = "[" + ",".join(str(x) for x in query_emb) + "]"

        async with UnitOfWork() as uow:
            # Get top relevant memories (lessons and patterns weighted higher)
            memories_result = await _session_execute(uow.session, text("""
                SELECT id, content, memory_type, salience,
                       1 - (semantic_embedding <=> CAST(:emb AS vector)) as similarity
                FROM memories
                WHERE NOT archived
                  AND 1 - (semantic_embedding <=> CAST(:emb AS vector)) > 0.45
                ORDER BY
                    CASE WHEN memory_type IN ('lesson', 'pattern') THEN 0.15 ELSE 0 END
                    + (1 - (semantic_embedding <=> CAST(:emb AS vector))) * 0.7
                    + (salience / 10.0) * 0.15
                    DESC
                LIMIT 5
            """), {"emb": emb_str})
            for row in memories_result.mappings().all():
                result["memories"].append({
                    "id": row["id"],
                    "content": row["content"][:300],
                    "type": row["memory_type"],
                    "salience": float(row["salience"]) if row["salience"] else 0,
                    "similarity": round(float(row["similarity"]), 3),
                })

            # 2. Get active guardrails from recent skill failures
            guardrails_result = await _session_execute(uow.session, text("""
                SELECT s.name, se.outcome_details, se.error_analysis, se.started_at
                FROM skill_executions se
                JOIN skills s ON s.id = se.skill_id
                WHERE se.outcome = 'failure'
                  AND se.started_at > NOW() - INTERVAL '7 days'
                ORDER BY se.started_at DESC
                LIMIT 3
            """))
            for row in guardrails_result.mappings().all():
                failure_text = row["error_analysis"] or row["outcome_details"] or "Unknown failure"
                result["guardrails"].append({
                    "skill": row["name"],
                    "failure": failure_text[:200],
                    "when": str(row["started_at"]),
                })

            # 3. Check for high-salience warnings (lessons with salience >= 9)
            warnings_result = await _session_execute(uow.session, text("""
                SELECT content, salience
                FROM memories
                WHERE memory_type IN ('lesson', 'pattern')
                  AND salience >= 9
                  AND NOT archived
                  AND 1 - (semantic_embedding <=> CAST(:emb AS vector)) > 0.5
                ORDER BY salience DESC, 1 - (semantic_embedding <=> CAST(:emb AS vector)) DESC
                LIMIT 2
            """), {"emb": emb_str})
            for row in warnings_result.mappings().all():
                result["warnings"].append(row["content"][:300])

    except Exception as e:
        result["error"] = str(e)

    # 4. Vault inventory — only list scoped secret names when a caller identity is known.
    try:
        from brain.systems.vault import async_list_secrets, async_get_missing_requests
        if user_id:
            vault_secrets = await async_list_secrets(user_id=user_id, org_id=org_id)
            vault_names_by_category = {}
            for s in vault_secrets:
                cat = s.get('category', 'general')
                vault_names_by_category.setdefault(cat, []).append(s['key_name'])
            result["vault_inventory"] = vault_names_by_category
        result["vault_missing"] = await async_get_missing_requests(user_id=user_id, org_id=org_id)
    except Exception:
        pass  # vault not available, skip

    return result


def get_context(
    message: str,
    *,
    user_id: str | None = None,
    org_id: str | None = None,
) -> dict:
    """Sync wrapper for CLI/hooks that cannot await the async context lookup."""
    return run_async_from_sync(
        async_get_context(message, user_id=user_id, org_id=org_id),
        thread_name="brain-context-sync-async-bridge",
    )


def format_system_message(ctx: dict) -> str:
    """Format brain context as a concise system message."""
    parts = []

    if ctx.get("warnings"):
        parts.append("⚠️ BRAIN WARNINGS:")
        for w in ctx["warnings"]:
            parts.append(f"  • {w}")

    if ctx.get("guardrails"):
        parts.append("🛡️ RECENT FAILURES:")
        for g in ctx["guardrails"]:
            parts.append(f"  • [{g['skill']}] {g['failure']}")

    if ctx.get("memories"):
        # Only show top 3 most relevant
        top = [m for m in ctx["memories"] if m["similarity"] > 0.5][:3]
        if top:
            parts.append("🧠 RELEVANT CONTEXT:")
            for m in top:
                parts.append(f"  • [{m['type']}, s:{m['salience']}] {m['content'][:150]}")

    if ctx.get("vault_inventory"):
        parts.append("🔐 VAULT (scoped secret names only):")
        for cat, names in ctx["vault_inventory"].items():
            parts.append(f"  • {cat}: {', '.join(names)}")
        parts.append("  → Never use os.environ.get() for secrets. Use brain_vault with a specific reason; user approval may be required.")
        parts.append("  → If a secret is missing, tell the user to add it in the Vault dashboard.")

    if ctx.get("vault_missing"):
        parts.append("⚠️ MISSING SECRETS (requested but not found):")
        for m in ctx["vault_missing"]:
            parts.append(f"  • {m['key_name']} — requested {m['request_count']}x, last: {m['last_requested']}")

    if not parts:
        return ""

    return "[Brain Context]\n" + "\n".join(parts)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 brain_context.py 'message text'", file=sys.stderr)
        sys.exit(1)

    message = sys.argv[1]
    ctx = get_context(message)
    system_msg = format_system_message(ctx)

    # Output JSON for the hook to parse
    print(json.dumps({
        "system_message": system_msg,
        "raw": ctx,
    }, default=str))
