#!/usr/bin/env python3
"""Session Hooks — wire the brain into the agent's daily workflow.

    session_hooks.py wake                # Load index + context
    session_hooks.py sense "message"     # Query context
    session_hooks.py encode "content"    # Encode a memory mid-session
    session_hooks.py sleep "summary"     # Encode session summary
    session_hooks.py status              # Quick health dashboard
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))  # repo root

import brain.kernel.config as config
from sqlalchemy import text

from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.platform.db.repositories.memory_visibility import (
    MemoryVisibilityContext,
    memory_visibility_sql,
)
from brain.systems.memory.embeddings import (
    embed_document,
    embed_query,
    server_health,
    vec_to_pg,
)
from brain.app.cli.memory import add_memory

# ============================================================
# Cross-channel recall
# ============================================================

async def get_cross_channel_context(
    current_session: str | None = None,
    hours: int = 24,
    limit: int = 20,
    *,
    user_id: str | None = None,
    org_id: str | None = None,
    allow_global: bool = False,
) -> list[dict]:
    """Get recent memories from other sessions/channels for cross-channel recall."""
    visibility_context = MemoryVisibilityContext(
        user_id=user_id,
        org_id=org_id,
        allow_global=allow_global,
    )
    vis_clause, vis_params = memory_visibility_sql(visibility_context, alias="")
    params = {"hours_interval": f"{hours} hours", "limit": limit, **vis_params}

    async with UnitOfWork() as uow:
        if current_session:
            params["current_session"] = current_session
            rows = (await uow.session.execute(text("""
                SELECT id, content, memory_type, salience, source_session,
                       source, created_at, tags
                FROM memories
                WHERE created_at >= NOW() - INTERVAL :hours_interval
                  AND source_session IS NOT NULL
                  AND source_session != :current_session
                  AND NOT archived
                  AND superseded_by IS NULL
                  {vis_clause}
                ORDER BY salience DESC, created_at DESC
                LIMIT :limit
            """.format(vis_clause=vis_clause)), params)).mappings().all()
        else:
            rows = (await uow.session.execute(text("""
                SELECT id, content, memory_type, salience, source_session,
                       source, created_at, tags
                FROM memories
                WHERE created_at >= NOW() - INTERVAL :hours_interval
                  AND source_session IS NOT NULL
                  AND NOT archived
                  AND superseded_by IS NULL
                  {vis_clause}
                ORDER BY salience DESC, created_at DESC
                LIMIT :limit
            """.format(vis_clause=vis_clause)), params)).mappings().all()

    return [
        {
            "id": row["id"],
            "content": row["content"][:300],
            "type": row["memory_type"],
            "memory_type": row["memory_type"],
            "salience": float(row["salience"]) if row["salience"] else 0,
            "source_session": row["source_session"],
            "source": row["source"],
            "created_at": row["created_at"].isoformat() if row["created_at"] else None,
            "tags": row["tags"] or [],
        }
        for row in rows
    ]


def _memory_scope_from_env() -> dict[str, str | bool | None]:
    """Resolve optional local CLI memory scope without guessing generic env vars."""
    return {
        "user_id": os.environ.get("ILLO_USER_ID"),
        "org_id": os.environ.get("ILLO_ORG_ID"),
        "allow_global": os.environ.get("ILLO_ALLOW_GLOBAL_MEMORY") == "1",
    }


# ============================================================
# Commands
# ============================================================


def _get_pending_review_prs() -> list[dict]:
    """Get PRs created by nightly that were NOT auto-merged (need human review)."""
    results_path = os.path.join(config.BRAIN_DIR, "logs", "nightly_results.json")
    if not os.path.exists(results_path):
        return []
    try:
        import json as _json
        results = _json.loads(open(results_path).read())
        pending = [
            {
                "pr_number": r["pr_number"],
                "confidence": r["confidence"],
                "reasoning_summary": r.get("reasoning_summary", ""),
                "timestamp": r.get("timestamp", ""),
            }
            for r in results
            if r.get("route") == "pr_for_review"
        ]
        return pending[-10:]  # Last 10 max
    except Exception:
        return []


async def cmd_wake(args):
    """Session start: load wake-up index + recent context."""
    pending_path = os.path.join(config.BRAIN_DIR, "PENDING_REFLECTION.json")
    pending_reflection = None
    if os.path.exists(pending_path):
        with open(pending_path) as f:
            pending_reflection = json.load(f)

    index_path = os.path.join(config.BRAIN_DIR, "WAKEUP_INDEX.md")
    index_content = ""
    if os.path.exists(index_path):
        with open(index_path) as f:
            index_content = f.read()

    async with UnitOfWork() as uow:
        skills = (await uow.session.execute(text("""
            SELECT name, maturity, confidence, use_count,
                   CASE WHEN use_count > 0 THEN ROUND(success_count::numeric/use_count*100,0) ELSE 0 END as pct
            FROM skills WHERE NOT archived ORDER BY use_count DESC
        """))).mappings().all()

    # Morning brief
    briefs_dir = os.path.join(config.BRAIN_DIR, "briefs")
    morning_brief = None
    if os.path.isdir(briefs_dir):
        for delta in [0, 1]:
            d = (datetime.now() - timedelta(days=delta)).strftime("%Y-%m-%d")
            path = os.path.join(briefs_dir, f"{d}.md")
            if os.path.exists(path):
                with open(path) as f:
                    morning_brief = {"date": d, "content": f.read()[:2000]}
                break

    # Cross-channel context
    session_id = os.environ.get("ILLO_SESSION_ID", "")
    cross_channel = await get_cross_channel_context(session_id, hours=24, **_memory_scope_from_env())

    # Experiment tracking
    experiment_info = await _get_experiment_info()

    # Self-audit and trust level
    self_audit = _run_self_audit()
    trust_info = _get_trust_info()

    # Nightly changes: auto-merged commits and dream memories
    nightly_changes = _get_nightly_changes()
    dream_memories = await _get_dream_memories()
    pending_review_prs = _get_pending_review_prs()

    print(json.dumps({
        "pending_reflection": pending_reflection,
        "index": index_content[:3000],
        "skills": [
            {"name": s["name"], "maturity": s["maturity"], "confidence": float(s["confidence"]) if s["confidence"] else 0,
             "uses": s["use_count"], "success_pct": float(s["pct"]) if s["pct"] else 0}
            for s in skills
        ],
        "morning_brief": morning_brief,
        "cross_channel_context": cross_channel,
        "self_audit": self_audit,
        "trust_level": trust_info,
        "nightly_changes": nightly_changes,
        "dreams": dream_memories,
        "pending_review_prs": pending_review_prs,
        "active_experiments": experiment_info["active_count"],
        "experiments_assessed_last_night": experiment_info["assessed_last_night"],
        "experiments_due_soon": experiment_info["due_soon"],
    }, indent=2, default=str))


async def _get_experiment_info() -> dict:
    """Get experiment tracking info for wake output."""
    import json as _json
    from datetime import date as _date
    result = {"active_count": 0, "assessed_last_night": [], "due_soon": []}
    try:
        async with UnitOfWork() as uow:
            rows = (await uow.session.execute(text("""
                SELECT id, content FROM memories
                WHERE memory_type = 'experiment' AND NOT archived
                ORDER BY created_at DESC
            """))).mappings().all()

        today = _date.today()
        soon_threshold = (today + timedelta(days=3)).isoformat()
        yesterday = (today - timedelta(days=1)).isoformat()

        for row in rows:
            content = row["content"]
            meta = {}
            if "EXPERIMENT_META:" in content:
                try:
                    meta_start = content.index("EXPERIMENT_META:") + 16
                    meta = _json.loads(content[meta_start:].strip())
                except (ValueError, _json.JSONDecodeError):
                    pass

            status = meta.get("status", "")
            hypothesis = meta.get("hypothesis", content[:80])
            assess_by = meta.get("assess_by", "")
            assessed_on = meta.get("assessed_on", "")

            if status == "active":
                result["active_count"] += 1
                if assess_by and assess_by <= soon_threshold:
                    result["due_soon"].append({
                        "hypothesis": hypothesis[:100],
                        "assess_by": assess_by,
                    })

            if assessed_on and assessed_on >= yesterday:
                result["assessed_last_night"].append({
                    "hypothesis": hypothesis[:100],
                    "verdict": meta.get("status", "unknown"),
                })
    except Exception:
        pass
    return result


def _get_nightly_changes() -> list[str]:
    """Get auto-merged commits from last nightly cycle."""
    try:
        result = subprocess.run(
            ['git', 'log', '--oneline', '--since=yesterday 3am', '--author=illo'],
            capture_output=True, text=True, timeout=5,
            cwd=str(config.BRAIN_DIR),
        )
        if result.returncode == 0 and result.stdout.strip():
            return [l.strip() for l in result.stdout.strip().split('\n') if l.strip()]
    except Exception:
        pass
    return []


async def _get_dream_memories() -> list[dict]:
    """Get dream memories from the last nightly cycle."""
    try:
        async with UnitOfWork() as uow:
            rows = (await uow.session.execute(text("""
                SELECT id, content, created_at
                FROM memories
                WHERE memory_type = 'dream'
                  AND created_at >= CURRENT_DATE - INTERVAL '1 day'
                  AND NOT archived
                ORDER BY created_at DESC
                LIMIT 5
            """))).mappings().all()
            return [{"id": r["id"], "content": r["content"][:200],
                     "created": str(r["created_at"])} for r in rows]
    except Exception:
        return []


def _run_self_audit():
    """Run self-audit on last night's automated runs."""
    try:
        from brain.systems.quality.validate import audit_last_night, format_audit_report
        audit = audit_last_night()
        if not audit["all_passed"]:
            return {
                "date": audit["date"],
                "total_issues": audit["total_issues"],
                "critical_issues": audit["critical_issues"],
                "summary": format_audit_report(audit),
            }
    except Exception:
        pass
    return None


def _get_trust_info():
    """Get current trust state from guardian."""
    try:
        from brain.systems.quality.guardian import get_trust_level, get_scout_checklist
        trust = get_trust_level()
        trust["checklist"] = get_scout_checklist()
        return trust
    except Exception:
        return None


async def _sense_context(message: str, timeout_s: int = 30) -> list:
    """Semantic context retrieval with graceful timeout. Returns context list."""
    import signal

    if len(message) <= 20:
        return []

    context = []

    class _Timeout(Exception):
        pass

    def _alarm(signum, frame):
        raise _Timeout()

    old_handler = signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(timeout_s)
    try:
        qemb = embed_query(message)
        emb_str = vec_to_pg(qemb)
        async with UnitOfWork() as uow:
            rows = (await uow.session.execute(text("""
                SELECT id, content, memory_type, salience,
                       1 - (semantic_embedding <=> CAST(:emb AS vector)) as sim
                FROM memories WHERE NOT archived AND superseded_by IS NULL
                ORDER BY semantic_embedding <=> CAST(:emb AS vector) LIMIT 5
            """), {"emb": emb_str})).mappings().all()

            for row in rows:
                if row["sim"] > 0.35:
                    context.append({
                        "id": row["id"], "content": row["content"][:200],
                        "type": row["memory_type"], "salience": row["salience"],
                        "similarity": round(row["sim"], 3),
                    })

            await uow.session.execute(text("""
                INSERT INTO retrieval_log (query_text, results_returned, top_result_id, top_score)
                VALUES (:query_text, :results_returned, :top_result_id, :top_score)
            """), {
                "query_text": message[:200],
                "results_returned": len(context),
                "top_result_id": context[0]["id"] if context else None,
                "top_score": context[0]["similarity"] if context else None,
            })
    except _Timeout:
        print(json.dumps({"warning": "context retrieval timed out (embed server cold start?)", "timeout_s": timeout_s}), file=sys.stderr)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old_handler)

    return context


async def cmd_sense(args):
    """Process incoming message and retrieve semantic context."""
    message = args.message
    timeout_s = getattr(args, "timeout", 30) or 30
    context = await _sense_context(message, timeout_s=timeout_s)

    output = {
        "relevant_context": context,
        "context_count": len(context),
    }

    print(json.dumps(output, indent=2, default=str))


async def cmd_sense_context(args):
    """Standalone context retrieval — use when embed server is known to be warm."""
    timeout_s = getattr(args, "timeout", 60) or 60
    context = await _sense_context(args.message, timeout_s=timeout_s)
    print(json.dumps({"relevant_context": context, "context_count": len(context)}, indent=2, default=str))


async def cmd_encode(args):
    """Quick-encode a memory during a session."""
    tags = [t.strip() for t in args.tags.split(",")] if args.tags else None
    related = [int(x.strip()) for x in args.related.split(",")] if args.related else None
    result = await add_memory(
        content=args.content, memory_type=args.type or "episode",
        salience=args.salience or 5.0,
        tags=tags, source="session", related_ids=related,
        decay_eligible=not (args.no_decay or False),
        source_session=getattr(args, "source_session", None),
    )
    print(json.dumps(result, indent=2))


async def cmd_log_retrieval(args):
    """Log retrieval feedback and adjust memory salience."""
    from retrieval_feedback import apply_retrieval_feedback
    async with UnitOfWork() as uow:
        # Find the most recent retrieval log entry
        row = (await uow.session.execute(text(
            "SELECT id FROM retrieval_log ORDER BY timestamp DESC LIMIT 1"
        ))).mappings().first()
        if not row:
            print(json.dumps({"logged": False, "error": "no retrieval_log entries"}))
            return
        result = apply_retrieval_feedback(row["id"], args.feedback, cur=uow.session)
    print(json.dumps({"logged": True, **result}))


async def cmd_sleep(args):
    """Session end: encode session summary."""
    async with UnitOfWork() as uow:
        if args.summary:
            semantic_emb = embed_document(args.summary)
            await uow.session.execute(text("""
                INSERT INTO memories (content, memory_type, semantic_embedding,
                                    salience, source, tags)
                VALUES (:content, 'episode', CAST(:semantic_emb AS vector), 5.0, 'session-end', :tags)
            """), {
                "content": args.summary,
                "semantic_emb": vec_to_pg(semantic_emb),
                "tags": [datetime.now().strftime("%Y-%m-%d")],
            })

        ret = (await uow.session.execute(text("""
            SELECT COUNT(*) as total,
                   COUNT(*) FILTER (WHERE feedback = 'hit') as hits,
                   COUNT(*) FILTER (WHERE feedback = 'miss') as misses
            FROM retrieval_log WHERE timestamp::date = CURRENT_DATE
        """))).mappings().first()

    print(json.dumps({
        "session_encoded": bool(args.summary),
        "retrievals_today": ret["total"], "retrieval_hits": ret["hits"], "retrieval_misses": ret["misses"],
    }))


async def cmd_status(args):
    """Quick health dashboard."""
    async with UnitOfWork() as uow:
        mem_row = (await uow.session.execute(text(
            "SELECT COUNT(*) as c FROM memories WHERE NOT archived"
        ))).mappings().first()
        mem_count = mem_row["c"]
        edge_row = (await uow.session.execute(text("SELECT COUNT(*) as c FROM edges"))).mappings().first()
        edge_count = edge_row["c"]

        skill_rows = (await uow.session.execute(text(
            "SELECT maturity, COUNT(*) as cnt FROM skills WHERE NOT archived GROUP BY maturity"
        ))).mappings().all()
        skill_maturity = {r["maturity"]: r["cnt"] for r in skill_rows}

        ret = (await uow.session.execute(text("""
            SELECT COUNT(*) as total, COUNT(*) FILTER (WHERE feedback = 'hit') as hits
            FROM retrieval_log WHERE timestamp >= CURRENT_DATE - INTERVAL '7 days'
        """))).mappings().first()

        last_consol = (await uow.session.execute(text(
            "SELECT run_date, status FROM consolidation_runs ORDER BY started_at DESC LIMIT 1"
        ))).mappings().first()

    health = server_health()
    embed_status = f"up ({health['uptime_s']}s, shuts down in {health.get('shutdown_in_s', '?')}s)" if health else "off (starts on demand)"

    print(json.dumps({
        "brain_health": {"memories": mem_count, "edges": edge_count, "embed_server": embed_status},
        "skills": {"total": sum(skill_maturity.values()), "by_maturity": skill_maturity},
        "retrieval_quality_7d": {"attempts": ret["total"], "hit_rate_pct": round(ret["hits"] / max(ret["total"], 1) * 100)},
        "last_consolidation": {"date": str(last_consol["run_date"]) if last_consol else None, "status": last_consol["status"] if last_consol else None},
    }, indent=2, default=str))


# ============================================================
# Main
# ============================================================

async def main():
    parser = argparse.ArgumentParser(description="Illo Session Hooks")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("wake"); p.add_argument("--session", default=None, help="Current session key for cross-channel filtering")
    p = sub.add_parser("sense"); p.add_argument("message"); p.add_argument("--timeout", type=int, default=30)
    p = sub.add_parser("sense-context"); p.add_argument("message"); p.add_argument("--timeout", type=int, default=60)

    p = sub.add_parser("encode"); p.add_argument("content")
    p.add_argument("--type", "-t", default="episode"); p.add_argument("--salience", "-s", type=float, default=5.0)
    p.add_argument("--tags"); p.add_argument("--related")
    p.add_argument("--no-decay", action="store_true")
    p.add_argument("--source-session", dest="source_session", default=None, help="Session key for cross-channel tracking")

    p = sub.add_parser("log-retrieval"); p.add_argument("--feedback", required=True, choices=["hit", "miss", "partial"])
    p = sub.add_parser("sleep"); p.add_argument("summary", nargs="?")
    sub.add_parser("status")

    args = parser.parse_args()
    await {"wake": cmd_wake, "sense": cmd_sense,
           "sense-context": cmd_sense_context, "encode": cmd_encode,
           "log-retrieval": cmd_log_retrieval, "sleep": cmd_sleep, "status": cmd_status}[args.command](args)


if __name__ == "__main__":
    asyncio.run(main())
