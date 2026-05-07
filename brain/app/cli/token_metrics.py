#!/usr/bin/env python3
"""Token Metrics CLI — baseline and ongoing monitoring for cortex run costs.

Generates reports on token usage, cost distribution, context sizes, and
identifies optimization opportunities.

Usage:
    python3 -m brain.app.cli.token_metrics report [--days 7]
    python3 -m brain.app.cli.token_metrics daily [--days 7]
    python3 -m brain.app.cli.token_metrics wasteful [--days 7] [--threshold 40000]
    python3 -m brain.app.cli.token_metrics baseline
    python3 -m brain.app.cli.token_metrics backfill-sessions
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))

from sqlalchemy import text

from brain.kernel import config
from brain.platform.db.repositories.unit_of_work import UnitOfWork

# Legacy session paths (kept for backfill compatibility)
SESSION_DIR = os.path.expanduser(
    os.environ.get("ILLO_LEGACY_SESSION_DIR", "~/.illo/agents/main/sessions")
)
SESSION_STORE = os.path.join(SESSION_DIR, "sessions.json")


def report(days: int = 7) -> dict:
    """Comprehensive token usage report for cortex runs."""
    with UnitOfWork() as uow:
        # Overall stats
        overall = dict(uow.session.execute(text("""
            SELECT
                COUNT(*) as total_runs,
                COUNT(*) FILTER (WHERE tokens_total IS NOT NULL AND tokens_total > 0) as with_token_data,
                COUNT(*) FILTER (WHERE tokens_total IS NULL OR tokens_total = 0) as missing_token_data,
                COUNT(*) FILTER (WHERE status = 'completed') as completed,
                COUNT(*) FILTER (WHERE status = 'failed') as failed,
                COUNT(*) FILTER (WHERE status = 'timeout') as timed_out,
                COALESCE(SUM(tokens_total), 0) as total_tokens,
                COALESCE(SUM(tokens_input), 0) as total_input,
                COALESCE(SUM(tokens_output), 0) as total_output,
                COALESCE(SUM(cache_read), 0) as total_cache_read,
                COALESCE(SUM(cache_write), 0) as total_cache_write,
                ROUND(AVG(tokens_total)::numeric, 0) as avg_tokens_total,
                ROUND(AVG(tokens_input)::numeric, 0) as avg_tokens_input,
                ROUND(AVG(tokens_output)::numeric, 0) as avg_tokens_output,
                MAX(tokens_total) as max_tokens_total,
                MIN(tokens_total) FILTER (WHERE tokens_total > 0) as min_tokens_total,
                ROUND(SUM(estimated_cost)::numeric, 4) as total_cost,
                ROUND(AVG(estimated_cost)::numeric, 4) as avg_cost
            FROM agent_runs
            WHERE created_at >= NOW() - INTERVAL '1 day' * :days
        """), {"days": days}).mappings().first())

        # Token tracking coverage
        total = overall["total_runs"]
        with_data = overall["with_token_data"]
        coverage_pct = round(100 * with_data / max(total, 1), 1)

        # Per-model breakdown
        by_model = [dict(r) for r in uow.session.execute(text("""
            SELECT
                model_used,
                COUNT(*) as runs,
                ROUND(AVG(tokens_total)::numeric, 0) as avg_tokens,
                ROUND(AVG(tokens_input)::numeric, 0) as avg_input,
                ROUND(AVG(tokens_output)::numeric, 0) as avg_output,
                COALESCE(SUM(tokens_total), 0) as total_tokens,
                ROUND(SUM(estimated_cost)::numeric, 4) as total_cost,
                ROUND(AVG(estimated_cost)::numeric, 4) as avg_cost
            FROM agent_runs
            WHERE created_at >= NOW() - INTERVAL '1 day' * :days
              AND tokens_total IS NOT NULL AND tokens_total > 0
              AND model_used IS NOT NULL
            GROUP BY model_used
            ORDER BY total_cost DESC
        """), {"days": days}).mappings().all()]

        # Per-skill breakdown
        by_skill = [dict(r) for r in uow.session.execute(text("""
            SELECT
                skill_used,
                COUNT(*) as runs,
                ROUND(AVG(tokens_total)::numeric, 0) as avg_tokens,
                ROUND(AVG(tokens_input)::numeric, 0) as avg_input,
                COALESCE(SUM(tokens_total), 0) as total_tokens,
                ROUND(SUM(estimated_cost)::numeric, 4) as total_cost,
                COUNT(*) FILTER (WHERE skill_outcome = 'success') as successes,
                COUNT(*) FILTER (WHERE skill_outcome = 'failure') as failures
            FROM agent_runs
            WHERE created_at >= NOW() - INTERVAL '1 day' * :days
              AND skill_used IS NOT NULL
            GROUP BY skill_used
            ORDER BY total_cost DESC
        """), {"days": days}).mappings().all()]

        # Cache efficiency
        cache = dict(uow.session.execute(text("""
            SELECT
                ROUND(AVG(
                    CASE WHEN (cache_read + cache_write) > 0
                    THEN 100.0 * cache_read / (cache_read + cache_write)
                    ELSE 0 END
                )::numeric, 1) as avg_cache_hit_pct,
                ROUND(AVG(cache_read)::numeric, 0) as avg_cache_read,
                ROUND(AVG(cache_write)::numeric, 0) as avg_cache_write
            FROM agent_runs
            WHERE created_at >= NOW() - INTERVAL '1 day' * :days
              AND cache_read IS NOT NULL
              AND tokens_total > 0
        """), {"days": days}).mappings().first())

        # Top 5 most expensive runs
        top_expensive = [dict(r) for r in uow.session.execute(text("""
            SELECT
                id, skill_used, model_used, tokens_total, tokens_input,
                tokens_output, estimated_cost, status,
                EXTRACT(EPOCH FROM (COALESCE(completed_at, NOW()) - started_at))::int as duration_sec,
                created_at
            FROM agent_runs
            WHERE created_at >= NOW() - INTERVAL '1 day' * :days
              AND tokens_total > 0
            ORDER BY tokens_total DESC
            LIMIT 5
        """), {"days": days}).mappings().all()]

    return {
        "period_days": days,
        "overall": overall,
        "token_tracking_coverage_pct": coverage_pct,
        "by_model": by_model,
        "by_skill": by_skill,
        "cache_efficiency": cache,
        "top_5_largest": top_expensive,
    }


def daily_breakdown(days: int = 7) -> list[dict]:
    """Day-by-day token usage and cost."""
    with UnitOfWork() as uow:
        rows = uow.session.execute(text("""
            SELECT
                created_at::date as day,
                COUNT(*) as runs,
                COUNT(*) FILTER (WHERE tokens_total > 0) as with_tokens,
                COALESCE(SUM(tokens_total), 0) as total_tokens,
                COALESCE(SUM(tokens_input), 0) as total_input,
                COALESCE(SUM(tokens_output), 0) as total_output,
                ROUND(AVG(tokens_total)::numeric, 0) as avg_tokens,
                ROUND(SUM(estimated_cost)::numeric, 4) as total_cost,
                MAX(tokens_total) as max_run_tokens
            FROM agent_runs
            WHERE created_at >= NOW() - INTERVAL '1 day' * :days
            GROUP BY created_at::date
            ORDER BY day DESC
        """), {"days": days}).mappings().all()
        return [dict(r) for r in rows]


def find_wasteful(days: int = 7, threshold: int = 40000) -> list[dict]:
    """Find runs with unusually high token usage — optimization targets."""
    with UnitOfWork() as uow:
        rows = uow.session.execute(text("""
            SELECT
                id, idea_id, skill_used, model_used,
                tokens_total, tokens_input, tokens_output,
                cache_read, cache_write, estimated_cost,
                status, event,
                EXTRACT(EPOCH FROM (COALESCE(completed_at, NOW()) - started_at))::int as duration_sec,
                created_at,
                LEFT(message, 200) as message_preview
            FROM agent_runs
            WHERE created_at >= NOW() - INTERVAL '1 day' * :days
              AND tokens_total > :threshold
            ORDER BY tokens_total DESC
            LIMIT 20
        """), {"days": days, "threshold": threshold}).mappings().all()
        return [dict(r) for r in rows]


def baseline_snapshot() -> dict:
    """Capture a baseline snapshot for future comparison.

    Records current averages so we can measure the impact of optimizations.
    Saves to the runtime-private token baseline path.
    """
    report_data = report(days=7)
    daily_data = daily_breakdown(days=7)

    baseline = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "period_days": 7,
        "metrics": {
            "avg_tokens_per_run": report_data["overall"]["avg_tokens_total"],
            "avg_input_per_run": report_data["overall"]["avg_tokens_input"],
            "avg_output_per_run": report_data["overall"]["avg_tokens_output"],
            "avg_cost_per_run": float(report_data["overall"]["avg_cost"]) if report_data["overall"]["avg_cost"] else 0,
            "total_cost_7d": float(report_data["overall"]["total_cost"]) if report_data["overall"]["total_cost"] else 0,
            "total_runs_7d": report_data["overall"]["total_runs"],
            "token_tracking_coverage_pct": report_data["token_tracking_coverage_pct"],
            "cache_hit_pct": float(report_data["cache_efficiency"]["avg_cache_hit_pct"]) if report_data["cache_efficiency"]["avg_cache_hit_pct"] else 0,
        },
        "by_model": report_data["by_model"],
        "daily": [
            {
                "day": str(d["day"]),
                "runs": d["runs"],
                "total_tokens": d["total_tokens"],
                "total_cost": float(d["total_cost"]) if d["total_cost"] else 0,
            }
            for d in daily_data
        ],
    }

    # Save to file
    baseline_path = str(Path(config.BRAIN_DIR) / "brain" / "platform" / "data" / "token_baseline.json")
    os.makedirs(os.path.dirname(baseline_path), exist_ok=True)
    with open(baseline_path, "w") as f:
        json.dump(baseline, f, indent=2, default=str)

    return baseline


def backfill_from_sessions() -> dict:
    """Backfill token data for runs that have null tokens.

    Reads from agent_sessions DB table (coordinator sessions).
    Falls back to a legacy local session store if available.
    """
    fixed = 0
    skipped = 0

    with UnitOfWork() as uow:
        # Find runs missing token data
        missing = uow.session.execute(text("""
            SELECT id, idea_id
            FROM agent_runs
            WHERE (tokens_total IS NULL OR tokens_total = 0)
              AND status IN ('completed', 'failed')
        """)).mappings().all()

        for row in missing:
            run_id = row["id"]
            idea_id = str(row["idea_id"])

            # Try to find matching agent session in DB
            session_id = f"coordinator-idea-{idea_id}"
            session = uow.session.execute(text(
                "SELECT total_input_tokens, total_output_tokens, total_cache_read, total_cache_creation "
                "FROM agent_sessions WHERE session_id = :sid"
            ), {"sid": session_id}).mappings().first()

            if not session or not (session["total_input_tokens"] or session["total_output_tokens"]):
                skipped += 1
                continue

            tokens_input = session["total_input_tokens"] or 0
            tokens_output = session["total_output_tokens"] or 0
            tokens_total = tokens_input + tokens_output
            cache_read = session["total_cache_read"] or 0
            cache_write = session["total_cache_creation"] or 0

            # Calculate cost
            model_row = uow.session.execute(text(
                "SELECT model_used FROM agent_runs WHERE id = :id"
            ), {"id": run_id}).mappings().first()
            model = model_row["model_used"] if model_row else None

            estimated_cost = None
            if model:
                try:
                    from brain.systems.runs.modeling import calculate_cost
                    estimated_cost = calculate_cost(model, tokens_input, tokens_output)
                except Exception:
                    pass

            uow.session.execute(text(
                "UPDATE agent_runs SET tokens_input = :ti, tokens_output = :to, "
                "tokens_total = :tt, cache_read = :cr, cache_write = :cw, estimated_cost = :cost "
                "WHERE id = :id"
            ), {
                "ti": tokens_input, "to": tokens_output, "tt": tokens_total,
                "cr": cache_read, "cw": cache_write, "cost": estimated_cost,
                "id": run_id,
            })
            fixed += 1

    return {"fixed": fixed, "skipped": skipped, "total_missing": len(missing)}


# ── CLI ───────────────────────────────────────────────────────

def cmd_report(args):
    data = report(days=args.days)
    print(json.dumps(data, indent=2, default=str))


def cmd_daily(args):
    data = daily_breakdown(days=args.days)
    print(json.dumps(data, indent=2, default=str))


def cmd_wasteful(args):
    data = find_wasteful(days=args.days, threshold=args.threshold)
    print(json.dumps(data, indent=2, default=str))


def cmd_baseline(args):
    data = baseline_snapshot()
    print(json.dumps(data, indent=2, default=str))
    baseline_path = str(Path(config.BRAIN_DIR) / "brain" / "platform" / "data" / "token_baseline.json")
    print(f"\nBaseline saved to: {baseline_path}", file=sys.stderr)


def cmd_backfill(args):
    data = backfill_from_sessions()
    print(json.dumps(data, indent=2, default=str))


def main():
    parser = argparse.ArgumentParser(description="Token Metrics CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("report", help="Comprehensive token usage report")
    p.add_argument("--days", type=int, default=7)

    p = sub.add_parser("daily", help="Day-by-day breakdown")
    p.add_argument("--days", type=int, default=7)

    p = sub.add_parser("wasteful", help="Find high-token runs")
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--threshold", type=int, default=40000)

    sub.add_parser("baseline", help="Capture baseline snapshot")

    sub.add_parser("backfill-sessions", help="Backfill tokens from session store")

    args = parser.parse_args()
    {
        "report": cmd_report,
        "daily": cmd_daily,
        "wasteful": cmd_wasteful,
        "baseline": cmd_baseline,
        "backfill-sessions": cmd_backfill,
    }[args.command](args)


if __name__ == "__main__":
    main()
