#!/usr/bin/env python3
"""
Brain Context — Returns relevant memories and guardrails for an incoming message.

Called by optional session integrations when a new message needs memory context.
Output is injected as a system message so the agent cannot ignore it.

Usage:
    python3 brain_context.py "user message text"

Output: JSON with relevant_memories, guardrails, and warnings.
"""

import asyncio
import json
import os
import sys
import inspect
from typing import Any

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))

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
    from brain.systems.reconstructive_memory.controller import reconstruct_memory

    user_id = user_id or os.environ.get("BRAIN_USER_ID")
    org_id = org_id or os.environ.get("BRAIN_ORG_ID")

    result = {
        "memories": [],
        "guardrails": [],
        "warnings": [],
    }

    # 1. Reconstruct relevant source-backed memory evidence.
    try:
        async with UnitOfWork() as uow:
            pack = await reconstruct_memory(
                uow.session,
                query=message,
                user_id=user_id,
                org_id=org_id,
                limit=5,
            )
            for item in pack.supporting_evidence:
                result["memories"].append({
                    "id": item.node_id,
                    "content": (item.source_text or item.text)[:300],
                    "type": "reconstructed_evidence",
                    "salience": round(float(item.confidence) * 10, 2),
                    "similarity": round(float(item.confidence), 3),
                    "source_span_id": item.source_span_id,
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

            # 3. Check for high-confidence warnings (lessons/patterns/procedures).
            warnings_result = await _session_execute(uow.session, text("""
                SELECT COALESCE(text, canonical_label) AS content, confidence
                FROM memory_nodes
                WHERE COALESCE(content_kind, node_kind) IN ('lesson', 'pattern', 'procedure', 'policy')
                  AND confidence >= 0.9
                  AND archived_at IS NULL
                ORDER BY confidence DESC, updated_at DESC
                LIMIT 2
            """))
            for row in warnings_result.mappings().all():
                result["warnings"].append(row["content"][:300])

    except Exception as e:
        result["error"] = str(e)

    # 4. Vault inventory — only list scoped secret names when a caller identity is known.
    try:
        from brain.systems.vault import async_list_secrets, async_get_missing_requests
        if user_id:
            vault_secrets = await async_list_secrets(actor_user_id=user_id, org_id=org_id)
            vault_names_by_category = {}
            for s in vault_secrets:
                cat = s.get('category', 'general')
                vault_names_by_category.setdefault(cat, []).append(s['key_name'])
            result["vault_inventory"] = vault_names_by_category
        result["vault_missing"] = await async_get_missing_requests(actor_user_id=user_id, org_id=org_id)
    except Exception:
        pass  # vault not available, skip

    return result


def get_context(
    message: str,
    *,
    user_id: str | None = None,
    org_id: str | None = None,
) -> dict:
    """Sync facade for legacy hooks that cannot yet await the async lookup."""
    with asyncio.Runner() as runner:
        return runner.run(async_get_context(message, user_id=user_id, org_id=org_id))


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
        parts.append(
            "  → Never use os.environ.get() for secrets. For CLI/API commands, mount Vault keys with "
            "exec_command/run_script secret_env; brain_vault returns a reference, not a raw token."
        )
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
    with asyncio.Runner() as runner:
        ctx = runner.run(async_get_context(message))
    system_msg = format_system_message(ctx)

    # Output JSON for the hook to parse
    print(json.dumps({
        "system_message": system_msg,
        "raw": ctx,
    }, default=str))
