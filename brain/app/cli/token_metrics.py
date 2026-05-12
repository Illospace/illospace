#!/usr/bin/env python3
"""Token Metrics CLI — baseline and ongoing monitoring for cortex run costs.

Generates reports on token usage, cost distribution, context sizes, and
identifies optimization opportunities.

Usage:
    python3 -m brain.app.cli.token_metrics report [--days 7]
    python3 -m brain.app.cli.token_metrics daily [--days 7]
    python3 -m brain.app.cli.token_metrics wasteful [--days 7] [--threshold 40000]
    python3 -m brain.app.cli.token_metrics baseline
"""

import argparse
from collections import defaultdict
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))

from brain.kernel import config
from brain.platform.db.repositories.unit_of_work import UnitOfWork, open_unit_of_work
from brain.systems.runs.token_usage import summarize_recent_run_usage


def _runs_for_period(days: int, *, limit: int = 10_000) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    with open_unit_of_work(UnitOfWork) as uow:
        return summarize_recent_run_usage(uow.session, limit=limit, since=since)


def _avg(values: list[int | float]) -> float:
    return sum(values) / len(values) if values else 0


def _run_cost(run: dict) -> float:
    return float(run.get("estimated_cost") or 0.0)


def _run_tokens(run: dict, key: str) -> int:
    return int(run.get(key) or 0)


def report(days: int = 7) -> dict:
    """Comprehensive token usage report for cortex runs."""
    runs = _runs_for_period(days)
    runs_with_tokens = [run for run in runs if _run_tokens(run, "tokens_total") > 0]

    total = len(runs)
    total_cost = sum(_run_cost(run) for run in runs)
    overall = {
        "total_runs": total,
        "with_token_data": len(runs_with_tokens),
        "missing_token_data": total - len(runs_with_tokens),
        "completed": sum(1 for run in runs if run.get("status") == "completed"),
        "failed": sum(1 for run in runs if run.get("status") == "failed"),
        "timed_out": sum(1 for run in runs if run.get("status") == "timeout"),
        "total_tokens": sum(_run_tokens(run, "tokens_total") for run in runs),
        "total_input": sum(_run_tokens(run, "tokens_input") for run in runs),
        "total_output": sum(_run_tokens(run, "tokens_output") for run in runs),
        "total_cache_read": sum(_run_tokens(run, "cache_read") for run in runs),
        "total_cache_write": sum(_run_tokens(run, "cache_write") for run in runs),
        "avg_tokens_total": round(_avg([_run_tokens(run, "tokens_total") for run in runs])),
        "avg_tokens_input": round(_avg([_run_tokens(run, "tokens_input") for run in runs])),
        "avg_tokens_output": round(_avg([_run_tokens(run, "tokens_output") for run in runs])),
        "max_tokens_total": max([_run_tokens(run, "tokens_total") for run in runs], default=0),
        "min_tokens_total": min([_run_tokens(run, "tokens_total") for run in runs_with_tokens], default=0),
        "total_cost": round(total_cost, 4),
        "avg_cost": round(total_cost / max(total, 1), 4),
    }

    coverage_pct = round(100 * len(runs_with_tokens) / max(total, 1), 1)

    model_map: dict[str, dict] = defaultdict(
        lambda: {
            "runs": 0,
            "total_tokens": 0,
            "total_input": 0,
            "total_output": 0,
            "total_cost": 0.0,
        }
    )
    for run in runs_with_tokens:
        model = run.get("model_used") or "unknown"
        bucket = model_map[model]
        bucket["runs"] += 1
        bucket["total_tokens"] += _run_tokens(run, "tokens_total")
        bucket["total_input"] += _run_tokens(run, "tokens_input")
        bucket["total_output"] += _run_tokens(run, "tokens_output")
        bucket["total_cost"] += _run_cost(run)
    by_model = sorted(
        [
            {
                "model_used": model,
                **bucket,
                "avg_tokens": round(bucket["total_tokens"] / max(bucket["runs"], 1)),
                "avg_input": round(bucket["total_input"] / max(bucket["runs"], 1)),
                "avg_output": round(bucket["total_output"] / max(bucket["runs"], 1)),
                "total_cost": round(bucket["total_cost"], 4),
                "avg_cost": round(bucket["total_cost"] / max(bucket["runs"], 1), 4),
            }
            for model, bucket in model_map.items()
        ],
        key=lambda item: item["total_cost"],
        reverse=True,
    )

    skill_map: dict[str, dict] = defaultdict(
        lambda: {
            "runs": 0,
            "total_tokens": 0,
            "total_input": 0,
            "total_cost": 0.0,
            "successes": 0,
            "failures": 0,
        }
    )
    for run in runs:
        skill = run.get("skill_used") or "unknown"
        bucket = skill_map[skill]
        bucket["runs"] += 1
        bucket["total_tokens"] += _run_tokens(run, "tokens_total")
        bucket["total_input"] += _run_tokens(run, "tokens_input")
        bucket["total_cost"] += _run_cost(run)
        if run.get("status") == "completed":
            bucket["successes"] += 1
        elif run.get("status") in {"failed", "error", "canceled", "cancelled"}:
            bucket["failures"] += 1
    by_skill = sorted(
        [
            {
                "skill_used": skill,
                **bucket,
                "avg_tokens": round(bucket["total_tokens"] / max(bucket["runs"], 1)),
                "avg_input": round(bucket["total_input"] / max(bucket["runs"], 1)),
                "total_cost": round(bucket["total_cost"], 4),
            }
            for skill, bucket in skill_map.items()
        ],
        key=lambda item: item["total_cost"],
        reverse=True,
    )

    cache_hit_values = [
        100.0 * _run_tokens(run, "cache_read")
        / max(_run_tokens(run, "cache_read") + _run_tokens(run, "cache_write"), 1)
        for run in runs_with_tokens
    ]
    cache = {
        "avg_cache_hit_pct": round(_avg(cache_hit_values), 1),
        "avg_cache_read": round(_avg([_run_tokens(run, "cache_read") for run in runs_with_tokens])),
        "avg_cache_write": round(_avg([_run_tokens(run, "cache_write") for run in runs_with_tokens])),
    }

    top_expensive = sorted(
        [
            {
                "id": run.get("id"),
                "thread_id": run.get("thread_id"),
                "skill_used": run.get("skill_used"),
                "model_used": run.get("model_used"),
                "tokens_total": _run_tokens(run, "tokens_total"),
                "tokens_input": _run_tokens(run, "tokens_input"),
                "tokens_output": _run_tokens(run, "tokens_output"),
                "estimated_cost": round(_run_cost(run), 4),
                "status": run.get("status"),
                "created_at": run.get("created_at"),
            }
            for run in runs_with_tokens
        ],
        key=lambda item: item["tokens_total"],
        reverse=True,
    )[:5]

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
    buckets: dict[str, dict] = defaultdict(
        lambda: {
            "runs": 0,
            "with_tokens": 0,
            "total_tokens": 0,
            "total_input": 0,
            "total_output": 0,
            "total_cost": 0.0,
            "max_run_tokens": 0,
        }
    )
    for run in _runs_for_period(days):
        created_at = run.get("created_at")
        day = created_at.date().isoformat() if created_at else "unknown"
        bucket = buckets[day]
        total_tokens = _run_tokens(run, "tokens_total")
        bucket["runs"] += 1
        bucket["with_tokens"] += 1 if total_tokens > 0 else 0
        bucket["total_tokens"] += total_tokens
        bucket["total_input"] += _run_tokens(run, "tokens_input")
        bucket["total_output"] += _run_tokens(run, "tokens_output")
        bucket["total_cost"] += _run_cost(run)
        bucket["max_run_tokens"] = max(bucket["max_run_tokens"], total_tokens)
    return [
        {
            "day": day,
            **bucket,
            "avg_tokens": round(bucket["total_tokens"] / max(bucket["runs"], 1)),
            "total_cost": round(bucket["total_cost"], 4),
        }
        for day, bucket in sorted(buckets.items(), reverse=True)
    ]


def find_wasteful(days: int = 7, threshold: int = 40000) -> list[dict]:
    """Find runs with unusually high token usage — optimization targets."""
    rows = [
        {
            "id": run.get("id"),
            "thread_id": run.get("thread_id"),
            "skill_used": run.get("skill_used"),
            "model_used": run.get("model_used"),
            "tokens_total": _run_tokens(run, "tokens_total"),
            "tokens_input": _run_tokens(run, "tokens_input"),
            "tokens_output": _run_tokens(run, "tokens_output"),
            "cache_read": _run_tokens(run, "cache_read"),
            "cache_write": _run_tokens(run, "cache_write"),
            "estimated_cost": round(_run_cost(run), 4),
            "status": run.get("status"),
            "event": run.get("event"),
            "created_at": run.get("created_at"),
        }
        for run in _runs_for_period(days)
        if _run_tokens(run, "tokens_total") > threshold
    ]
    return sorted(rows, key=lambda row: row["tokens_total"], reverse=True)[:20]


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

    args = parser.parse_args()
    {
        "report": cmd_report,
        "daily": cmd_daily,
        "wasteful": cmd_wasteful,
        "baseline": cmd_baseline,
    }[args.command](args)


if __name__ == "__main__":
    main()
