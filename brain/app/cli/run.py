#!/usr/bin/env python3
"""Illo Brain — deterministic task run helper.

Classifies tasks, selects templates, injects context from the brain,
and returns ready-to-run JSON payloads.

Usage:
    run.py run "task description" [--type TYPE] [--model MODEL] [--thinking LEVEL]
    run.py complete <run_id> --outcome success|failure|partial [--notes "..."]
    run.py history [--limit 10] [--type TYPE]
    run.py stats [--days 30]
    run.py replay <run_id>
"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))  # repo root

from sqlalchemy import text as sa_text

from brain.platform.db.repositories.unit_of_work import UnitOfWork
# ============================================================
# Inline Allowlist
# ============================================================

INLINE_PATTERNS = [
    r"memory\.py\s+(query|context)",
    r"skills\.py\s+plan",
    r"session_hooks\.py\s+(wake|sleep|encode|sense|status)",
    r"run\.py",
]

_INLINE_RE = [re.compile(p) for p in INLINE_PATTERNS]


def is_inline_task(task: str) -> bool:
    """Return True if this task should stay in the main session."""
    return any(r.search(task) for r in _INLINE_RE)


# ============================================================
# Task Classifier
# ============================================================

CLASSIFIERS = {
    "edit_file": lambda t: any(w in t for w in ["edit", "update", "change", "modify", "fix"]) and "file" in t,
    "investigate": lambda t: any(w in t for w in ["investigate", "debug", "why ", "trace", "find out", "look into"]),
    "implement": lambda t: any(w in t for w in ["implement", "build", "create", "add ", "write"]),
    "review": lambda t: any(w in t for w in ["review", "check", "audit", "verify"]),
    "encode": lambda t: any(w in t for w in ["encode", "remember", "save memory", "lesson"]),
}


def classify_task(task: str, explicit_type: str = None) -> str:
    """Classify a task description into a task type. <100ms, keyword-based."""
    if explicit_type:
        return explicit_type
    task_lower = task.lower()
    for task_type, matcher in CLASSIFIERS.items():
        if matcher(task_lower):
            return task_type
    return "custom"


# ============================================================
# Templates
# ============================================================

TEMPLATES = {
    "edit_file": {
        "model": "medium",
        "thinking": "low",
        "label_prefix": "edit",
        "prompt_template": """Edit the following file(s).

Task: {description}

{context_block}

Rules:
- Make the minimal change needed
- Run any existing tests after editing
- Report what you changed and test results
""",
    },
    "investigate": {
        "model": "medium",
        "thinking": "low",
        "label_prefix": "investigate",
        "prompt_template": """Investigate: {description}

{context_block}

Rules:
- Gather evidence first, then conclude
- Show actual data/values, not assumptions
- Report findings with confidence levels
""",
    },
    "implement": {
        "model": "medium",
        "thinking": "low",
        "label_prefix": "impl",
        "prompt_template": """Implement: {description}

{context_block}

Rules:
- Write tests first (TDD)
- Trace every code path end-to-end
- Run tests and paste output before declaring done
""",
    },
    "encode": {
        "model": "low",
        "thinking": "off",
        "label_prefix": "encode",
        "prompt_template": """Encode the following to the brain:

{description}

{context_block}

Run the appropriate session_hooks.py encode command and report the result.
""",
    },
    "review": {
        "model": "medium",
        "thinking": "low",
        "label_prefix": "review",
        "prompt_template": """Review: {description}

{context_block}

Apply the pre-flight checklist. Report issues found.
""",
    },
    "custom": {
        "model": "medium",
        "thinking": "low",
        "label_prefix": "task",
        "prompt_template": """{description}

{context_block}
""",
    },
}


# ============================================================
# Context Injection
# ============================================================

async def build_context_block(task: str, skill_name: str = None) -> tuple:
    """Query brain for context. Returns (context_text, metadata_dict)."""
    parts = []
    meta = {"memories": 0, "guardrails": 0, "similar_tasks": 0}

    # 1. Relevant memories via memory.py query_memories
    try:
        from brain.app.cli.memory import query_memories
        result = await query_memories(task, limit=5)
        memories = result.get("results", [])
        if memories:
            parts.append("## Relevant Memories")
            for m in memories:
                parts.append(f"- [{m['type']}] {m['content'][:200]}")
            meta["memories"] = len(memories)
    except Exception:
        pass

    # 2. Guardrails from skills.py plan (reuse existing function)
    try:
        from brain.systems.memory.embeddings import embed_query, vec_to_pg
        task_emb = embed_query(task)
        emb_str = vec_to_pg(task_emb)
        async with UnitOfWork() as uow:
            # Get guardrails from relevant memories (lessons/patterns)
            guardrail_rows = (await uow.session.execute(sa_text("""
                SELECT content, memory_type,
                       1 - (semantic_embedding <=> CAST(:emb AS vector)) as sim
                FROM memories
                WHERE NOT archived AND superseded_by IS NULL
                  AND memory_type IN ('lesson', 'pattern')
                ORDER BY semantic_embedding <=> CAST(:emb AS vector)
                LIMIT 5
            """), {"emb": emb_str})).mappings().all()
            guardrails = [r["content"][:200] for r in guardrail_rows if r["sim"] > 0.4]
            if guardrails:
                parts.append("## Guardrails (from past lessons)")
                for g in guardrails:
                    parts.append(f"- {g}")
                meta["guardrails"] = len(guardrails)
    except Exception:
        pass

    # 3. Similar past runs
    try:
        similar = await find_similar_runs(task, limit=3)
        if similar:
            parts.append("## Similar Past Tasks")
            for s in similar:
                parts.append(f"- [{s['outcome'] or 'pending'}] {s['task_summary'][:150]}")
            meta["similar_tasks"] = len(similar)
    except Exception:
        pass

    return "\n".join(parts), meta


async def find_similar_runs(task: str, limit: int = 3) -> list:
    """Find similar past runs by text similarity (simple ILIKE for now)."""
    async with UnitOfWork() as uow:
        # Use first few significant words for matching
        words = [w for w in task.lower().split() if len(w) > 3][:5]
        if not words:
            return []
        conditions = " OR ".join([f"input_message ILIKE :word_{i}" for i in range(len(words))])
        params = {f"word_{i}": f"%{w}%" for i, w in enumerate(words)}
        params["limit"] = limit
        rows = (await uow.session.execute(sa_text(f"""
            SELECT id,
                   input_message AS task_summary,
                   metadata->>'task_type' AS task_type,
                   COALESCE(metadata->>'outcome', status) AS outcome,
                   metadata->>'outcome_notes' AS outcome_notes
            FROM agent_runs
            WHERE {conditions}
              AND metadata->>'legacy_source' = 'cli.run'
            ORDER BY created_at DESC
            LIMIT :limit
        """), params)).mappings().all()
        return [dict(r) for r in rows]


# ============================================================
# Run Persistence
# ============================================================

async def log_run(
    session,
    task_summary: str,
    task_type: str,
    template_used: str,
    model: str,
    thinking_level: str,
    prompt_hash: str = None,
    payload_json: dict = None,
    skill_name: str = None,
    memories_injected: list = None,
    guardrails_injected: list = None,
    similar_past_ids: list = None,
    session_key: str = None,
) -> int:
    """Persist a CLI run in agent_runs and store its replay payload as an artifact."""
    metadata = {
        "legacy_source": "cli.run",
        "task_type": task_type,
        "template_used": template_used,
        "skill_name": skill_name,
        "thinking_level": thinking_level,
        "prompt_hash": prompt_hash,
        "memories_injected": memories_injected or [],
        "guardrails_injected": guardrails_injected or [],
        "similar_past_ids": similar_past_ids or [],
        "session_key": session_key,
    }
    row = (await session.execute(sa_text("""
        INSERT INTO agent_runs (
            thread_id, profile, recipe, status, input_message,
            target_ref, workspace_ref, model_policy, metadata
        ) VALUES (
            :thread_id, 'cli', 'run_helper', 'queued', :task_summary,
            CAST(:target_ref AS jsonb), '{}'::jsonb,
            CAST(:model_policy AS jsonb), CAST(:metadata AS jsonb)
        )
        RETURNING id
    """), {
        "thread_id": session_key or "cli-run",
        "task_summary": task_summary,
        "target_ref": json.dumps({"kind": "cli_task", "task_type": task_type}),
        "model_policy": json.dumps({"model": model, "thinking": thinking_level}),
        "metadata": json.dumps(metadata),
    })).mappings().first()
    run_id = int(row["id"])
    await session.execute(sa_text("""
        UPDATE agent_runs
        SET root_run_id = COALESCE(root_run_id, id),
            trace_id = COALESCE(trace_id, 'cli-' || id::text)
        WHERE id = :id
    """), {"id": run_id})
    await session.execute(sa_text("""
        INSERT INTO agent_run_artifacts (
            run_id, root_run_id, artifact_type, title, payload, text, visibility
        ) VALUES (
            :run_id, :run_id, 'run_payload', 'CLI run payload',
            CAST(:payload AS jsonb), :text, 'private'
        )
    """), {
        "run_id": run_id,
        "payload": json.dumps(payload_json or {}),
        "text": json.dumps(payload_json or {}, indent=2),
    })
    return run_id


# ============================================================
# Complete Hook
# ============================================================

async def complete_run(session, run_id: int, outcome: str, notes: str = None) -> dict:
    """Mark a CLI agent_run row as complete."""
    row = (await session.execute(sa_text("""
        SELECT id, metadata
        FROM agent_runs
        WHERE id = :id AND metadata->>'legacy_source' = 'cli.run'
    """), {"id": run_id})).mappings().first()
    if not row:
        return {"error": f"Run {run_id} not found"}

    status = {
        "success": "completed",
        "partial": "completed",
        "failure": "failed",
        "cancelled": "canceled",
    }.get(outcome, "completed")
    await session.execute(sa_text("""
        UPDATE agent_runs
        SET status = :status,
            completed_at = CASE WHEN :status = 'completed' THEN NOW() ELSE completed_at END,
            failed_at = CASE WHEN :status = 'failed' THEN NOW() ELSE failed_at END,
            canceled_at = CASE WHEN :status = 'canceled' THEN NOW() ELSE canceled_at END,
            metadata = metadata || CAST(:metadata_patch AS jsonb)
        WHERE id = :id
    """), {
        "status": status,
        "metadata_patch": json.dumps({"outcome": outcome, "outcome_notes": notes}),
        "id": run_id,
    })

    return {"run_id": run_id, "outcome": outcome}


# ============================================================
# Build Payload
# ============================================================

def _make_label(prefix: str, task: str) -> str:
    """Generate a short label from prefix + task words."""
    words = re.sub(r"[^a-z0-9\s]", "", task.lower()).split()
    significant = [w for w in words if len(w) > 3][:3]
    return f"{prefix}-{'-'.join(significant)}" if significant else f"{prefix}-task"


async def build_payload(
    task: str,
    task_type: str,
    model: str = None,
    thinking: str = None,
    skill_name: str = None,
) -> dict:
    """Build a complete spawn payload. Core function."""
    template = TEMPLATES[task_type]

    effective_model = model or template["model"]
    effective_thinking = thinking or template["thinking"]

    # Context injection
    context_text, context_meta = await build_context_block(task, skill_name)

    # Render prompt
    prompt = template["prompt_template"].format(
        description=task,
        context_block=context_text,
    )

    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()[:16]
    label = _make_label(template["label_prefix"], task)

    payload = {
        "label": label,
        "model": effective_model,
        "thinking": effective_thinking,
        "prompt": prompt,
        "task_type": task_type,
        "template": task_type,
        "context_injected": context_meta,
    }

    # Log to DB
    async with UnitOfWork() as uow:
        run_id = await log_run(
            session=uow.session,
            task_summary=task,
            task_type=task_type,
            template_used=task_type,
            model=effective_model,
            thinking_level=effective_thinking,
            prompt_hash=prompt_hash,
            payload_json=payload,
            skill_name=skill_name,
            memories_injected=list(range(context_meta["memories"])),
            guardrails_injected=[],
            similar_past_ids=list(range(context_meta["similar_tasks"])),
        )

    payload["run_id"] = run_id
    return payload


# ============================================================
# Main run entry point
# ============================================================

async def run(task: str, task_type: str = None, model: str = None, thinking: str = None) -> dict:
    """Classify, template, inject context, log, return payload."""
    classified = classify_task(task, explicit_type=task_type)
    return await build_payload(task, classified, model=model, thinking=thinking)


# ============================================================
# CLI Commands
# ============================================================

async def cmd_run(args):
    result = await run(args.task, task_type=args.type, model=args.model, thinking=args.thinking)
    print(json.dumps(result, indent=2))


async def cmd_complete(args):
    async with UnitOfWork() as uow:
        result = await complete_run(uow.session, args.run_id, args.outcome, args.notes)
    print(json.dumps(result, indent=2, default=str))


async def cmd_history(args):
    async with UnitOfWork() as uow:
        sql = """
            SELECT id,
                   input_message AS task_summary,
                   metadata->>'task_type' AS task_type,
                   COALESCE(metadata->>'outcome', status) AS outcome,
                   model_policy->>'model' AS model,
                   created_at AS runed_at,
                   completed_at,
                   EXTRACT(EPOCH FROM (completed_at - created_at))::INTEGER AS duration_s
            FROM agent_runs
            WHERE metadata->>'legacy_source' = 'cli.run'
        """
        params = {}
        if args.type:
            sql += " AND metadata->>'task_type' = :task_type"
            params["task_type"] = args.type
        sql += " ORDER BY runed_at DESC LIMIT :limit"
        params["limit"] = args.limit
        rows = (await uow.session.execute(sa_text(sql), params)).mappings().all()
    print(json.dumps([dict(r) for r in rows], indent=2, default=str))


async def cmd_stats(args):
    async with UnitOfWork() as uow:
        overview = dict((await uow.session.execute(sa_text("""
            SELECT
                COUNT(*) as total,
                COUNT(*) FILTER (WHERE metadata->>'outcome' = 'success') as successes,
                COUNT(*) FILTER (WHERE metadata->>'outcome' = 'failure') as failures,
                COUNT(*) FILTER (WHERE metadata->>'outcome' = 'partial') as partials,
                COUNT(*) FILTER (WHERE metadata->>'outcome' IS NULL) as pending,
                ROUND(AVG(EXTRACT(EPOCH FROM (completed_at - created_at)))::numeric, 0) as avg_duration_s
            FROM agent_runs
            WHERE metadata->>'legacy_source' = 'cli.run'
              AND created_at >= NOW() - INTERVAL '1 day' * :days
        """), {"days": args.days})).mappings().first())

        by_type_rows = (await uow.session.execute(sa_text("""
            SELECT metadata->>'task_type' AS task_type,
                   COUNT(*) as cnt,
                   COUNT(*) FILTER (WHERE metadata->>'outcome' = 'success') as ok
            FROM agent_runs
            WHERE metadata->>'legacy_source' = 'cli.run'
              AND created_at >= NOW() - INTERVAL '1 day' * :days
            GROUP BY task_type ORDER BY cnt DESC
        """), {"days": args.days})).mappings().all()
        by_type = [dict(r) for r in by_type_rows]

    print(json.dumps({"period_days": args.days, "overview": overview, "by_type": by_type}, indent=2, default=str))


async def cmd_replay(args):
    async with UnitOfWork() as uow:
        row = (await uow.session.execute(sa_text(
            """
            SELECT payload
            FROM agent_run_artifacts
            WHERE run_id = :id AND artifact_type = 'run_payload'
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """
        ), {"id": args.run_id})).mappings().first()
    if not row or not row["payload"]:
        print(json.dumps({"error": f"Run {args.run_id} not found or has no payload"}))
        return
    print(json.dumps(row["payload"], indent=2))


# ============================================================
# Argument Parser
# ============================================================

async def main():
    parser = argparse.ArgumentParser(description="Illo Brain — Run System")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run")
    p.add_argument("task")
    p.add_argument("--type", "-t", choices=list(TEMPLATES.keys()))
    p.add_argument("--model", "-m")
    p.add_argument("--thinking")

    p = sub.add_parser("complete")
    p.add_argument("run_id", type=int)
    p.add_argument("--outcome", required=True, choices=["success", "failure", "partial", "cancelled"])
    p.add_argument("--notes")

    p = sub.add_parser("history")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--type", "-t")

    p = sub.add_parser("stats")
    p.add_argument("--days", type=int, default=30)

    p = sub.add_parser("replay")
    p.add_argument("run_id", type=int)

    args = parser.parse_args()
    await {"run": cmd_run, "complete": cmd_complete, "history": cmd_history,
           "stats": cmd_stats, "replay": cmd_replay}[args.command](args)


if __name__ == "__main__":
    asyncio.run(main())
