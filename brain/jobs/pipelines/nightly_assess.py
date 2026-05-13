#!/usr/bin/env python3
"""Nightly experiment assessment — evaluates active experiments that are due.

Checks experiment memories for due assessments, gathers data from specified
sources, and updates experiment status (passed/failed/inconclusive).

Usage:
    python3 -m brain.jobs.pipelines.nightly_assess [--date 2026-03-04] [--dry-run]
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys
from datetime import date, timedelta

from sqlalchemy import text

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))
import brain.kernel.config as config
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.app.cli.memory import add_memory

PROJECT_ROOT = str(config.BRAIN_DIR)
LOG_DIR = str(config.BRAIN_LOG_DIR)
MAX_EXTENSIONS = 2
EXTENSION_DAYS = 7


def _log(msg: str):
    print(msg)


def _parse_experiment_metadata(content: str) -> dict:
    """Extract structured metadata from experiment memory content.

    Experiment memories store metadata as a JSON block after the description.
    Format: EXPERIMENT: <description>\n```json\n{...}\n```
    """
    meta = {}
    if "```json" in content:
        try:
            json_start = content.index("```json") + 7
            json_end = content.index("```", json_start)
            meta = json.loads(content[json_start:json_end].strip())
        except (ValueError, json.JSONDecodeError):
            pass
    elif "EXPERIMENT_META:" in content:
        try:
            meta_start = content.index("EXPERIMENT_META:") + 16
            meta = json.loads(content[meta_start:].strip())
        except (ValueError, json.JSONDecodeError):
            pass
    return meta


def _update_experiment_content(content: str, meta: dict) -> str:
    """Replace the metadata block in experiment content with updated metadata."""
    desc = content.split("```json")[0].split("EXPERIMENT_META:")[0].rstrip()
    return f"{desc}\nEXPERIMENT_META:{json.dumps(meta)}"


async def gather_due_experiments(target_date: date) -> list[dict]:
    """Query active experiment memories due for assessment."""
    date_str = target_date.isoformat()
    async with UnitOfWork() as uow:
        result = await uow.session.execute(text("""
            SELECT id, content, salience, tags, created_at
            FROM memories
            WHERE memory_type = 'experiment'
              AND NOT archived
            ORDER BY created_at DESC
        """))
        rows = [dict(r) for r in result.mappings().all()]

    due = []
    for row in rows:
        meta = _parse_experiment_metadata(row["content"])
        if meta.get("status") != "active":
            continue
        assess_by = meta.get("assess_by", "")
        if assess_by and assess_by <= date_str:
            row["meta"] = meta
            due.append(row)
    return due


async def gather_data(data_source: str) -> dict:
    """Gather quantitative data from a known data source.

    Returns: {"available": bool, "metrics": dict, "summary": str}
    """
    if data_source == "skill_success_rates":
        return await _gather_skill_stats()
    elif data_source == "nightly_logs":
        return _gather_nightly_log_stats()
    elif data_source == "test_results":
        return _gather_test_results()
    else:
        return {"available": False, "metrics": {}, "summary": f"Manual assessment needed for source: {data_source}"}


async def _gather_skill_stats() -> dict:
    """Query skills table for recent success rates."""
    try:
        async with UnitOfWork() as uow:
            result = await uow.session.execute(text("""
                SELECT name,
                       use_count,
                       success_count,
                       CASE WHEN use_count > 0
                            THEN ROUND(success_count::numeric / use_count * 100, 1)
                            ELSE 0 END as success_pct
                FROM skills
                WHERE NOT archived AND use_count > 0
                ORDER BY use_count DESC
                LIMIT 20
            """))
            rows = [dict(r) for r in result.mappings().all()]
        if not rows:
            return {"available": False, "metrics": {}, "summary": "No skill usage data"}
        avg_pct = sum(r["success_pct"] for r in rows) / len(rows)
        return {
            "available": True,
            "metrics": {"avg_success_pct": float(avg_pct), "skill_count": len(rows)},
            "summary": f"Avg skill success: {avg_pct:.1f}% across {len(rows)} skills",
        }
    except Exception as e:
        return {"available": False, "metrics": {}, "summary": f"Error querying skills: {e}"}


def _gather_nightly_log_stats() -> dict:
    """Check recent nightly logs for errors."""
    try:
        log_dir = LOG_DIR
        errors = 0
        total_lines = 0
        for delta in range(7):
            d = (date.today() - timedelta(days=delta)).isoformat()
            log_path = os.path.join(log_dir, f"nightly-{d}.log")
            if os.path.exists(log_path):
                with open(log_path) as f:
                    lines = f.readlines()
                total_lines += len(lines)
                errors += sum(1 for l in lines if "ERROR" in l or "❌" in l or "Traceback" in l)
        if total_lines == 0:
            return {"available": False, "metrics": {}, "summary": "No nightly logs found"}
        return {
            "available": True,
            "metrics": {"error_lines": errors, "total_lines": total_lines},
            "summary": f"{errors} error lines in {total_lines} total log lines (7 days)",
        }
    except Exception as e:
        return {"available": False, "metrics": {}, "summary": f"Error reading logs: {e}"}


def _gather_test_results() -> dict:
    """Run pytest and check pass rate."""
    try:
        r = subprocess.run(
            ["bash", "-c", "source venv/bin/activate && python3 -m pytest tests/ -q --tb=no"],
            capture_output=True, text=True, timeout=120,
            cwd=PROJECT_ROOT,
        )
        output = r.stdout + r.stderr
        # Parse "X passed, Y failed" from pytest output
        passed = failed = 0
        for line in output.split("\n"):
            if "passed" in line:
                import re
                m = re.search(r"(\d+) passed", line)
                if m:
                    passed = int(m.group(1))
                m = re.search(r"(\d+) failed", line)
                if m:
                    failed = int(m.group(1))
        total = passed + failed
        if total == 0:
            return {"available": False, "metrics": {}, "summary": "No test results parsed"}
        return {
            "available": True,
            "metrics": {"passed": passed, "failed": failed, "pass_rate": round(passed / total * 100, 1)},
            "summary": f"Tests: {passed}/{total} passed ({round(passed/total*100,1)}%)",
        }
    except Exception as e:
        return {"available": False, "metrics": {}, "summary": f"Error running tests: {e}"}


async def assess_single_experiment(experiment: dict, target_date: date, dry_run: bool = False) -> dict:
    """Assess a single experiment. Returns assessment result dict."""
    meta = experiment["meta"]
    mem_id = experiment["id"]
    hypothesis = meta.get("hypothesis", "unknown")
    data_source = meta.get("data_source", "")
    extensions = meta.get("extensions", 0)

    _log(f"\n📊 Assessing experiment #{mem_id}: {hypothesis}")

    # Gather data
    data = await gather_data(data_source)
    _log(f"  Data source '{data_source}': {data['summary']}")

    # Determine verdict
    if not data["available"]:
        if extensions < MAX_EXTENSIONS:
            verdict = "inconclusive"
            reason = f"No data available from '{data_source}', extending assessment period"
        else:
            verdict = "inconclusive"
            reason = f"No data after {extensions} extensions, marking final inconclusive"
    else:
        metrics = data["metrics"]
        # Simple heuristic assessment
        verdict = _heuristic_assess(data_source, metrics)
        reason = data["summary"]

    _log(f"  Verdict: {verdict} — {reason}")

    # Update the experiment memory
    meta["status"] = verdict
    meta["verdict"] = reason
    meta["assessed_on"] = target_date.isoformat()

    if verdict == "inconclusive" and extensions < MAX_EXTENSIONS:
        meta["extensions"] = extensions + 1
        meta["status"] = "active"  # Keep active for re-assessment
        new_assess_by = (target_date + timedelta(days=EXTENSION_DAYS)).isoformat()
        meta["assess_by"] = new_assess_by
        _log(f"  Extended assess_by to {new_assess_by} (extension {extensions + 1}/{MAX_EXTENSIONS})")

    if not dry_run:
        new_content = _update_experiment_content(experiment["content"], meta)
        async with UnitOfWork() as uow:
            await uow.session.execute(text(
                "UPDATE memories SET content = :content WHERE id = :id"
            ), {"content": new_content, "id": mem_id})

        # If failed and has PR number, create improvement memory suggesting revert
        if verdict == "failed" and meta.get("pr_number"):
            await add_memory(
                content=f"REVERT RECOMMENDATION: Experiment '{hypothesis}' failed. "
                        f"Consider reverting PR #{meta['pr_number']}. Reason: {reason}",
                memory_type="improvement",
                salience=7.0,
                tags=["experiment-revert", f"pr-{meta['pr_number']}"],
                source="nightly-assess",
            )
            _log(f"  Created revert recommendation for PR #{meta['pr_number']}")

    return {
        "memory_id": mem_id,
        "hypothesis": hypothesis,
        "verdict": verdict,
        "reason": reason,
        "data_source": data_source,
        "data_available": data["available"],
    }


def _heuristic_assess(data_source: str, metrics: dict) -> str:
    """Simple heuristic to determine pass/fail from metrics."""
    if data_source == "skill_success_rates":
        pct = metrics.get("avg_success_pct", 0)
        if pct >= 70:
            return "passed"
        elif pct < 50:
            return "failed"
        return "inconclusive"

    elif data_source == "nightly_logs":
        errors = metrics.get("error_lines", 0)
        total = metrics.get("total_lines", 1)
        error_rate = errors / max(total, 1)
        if error_rate < 0.01:
            return "passed"
        elif error_rate > 0.05:
            return "failed"
        return "inconclusive"

    elif data_source == "test_results":
        pass_rate = metrics.get("pass_rate", 0)
        if pass_rate >= 95:
            return "passed"
        elif pass_rate < 80:
            return "failed"
        return "inconclusive"

    return "inconclusive"


async def assess_experiments(target_date: date | None = None, dry_run: bool = False) -> list[dict]:
    """Main entry point: assess all due experiments."""
    target_date = target_date or date.today()
    _log(f"{'='*60}")
    _log(f"EXPERIMENT ASSESSMENT — {target_date} {'[DRY RUN]' if dry_run else ''}")
    _log(f"{'='*60}")

    due = await gather_due_experiments(target_date)
    if not due:
        _log("No experiments due for assessment.")
        return []

    _log(f"Found {len(due)} experiment(s) due for assessment")

    results = []
    for exp in due:
        result = await assess_single_experiment(exp, target_date, dry_run=dry_run)
        results.append(result)

    _log(f"\n{'='*60}")
    _log(f"Assessment complete: {len(results)} experiment(s) assessed")
    for r in results:
        _log(f"  • {r['hypothesis'][:60]} → {r['verdict']}")
    _log(f"{'='*60}")

    return results


def main():
    parser = argparse.ArgumentParser(description="Nightly experiment assessment")
    parser.add_argument("--date", help="Target date (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done")
    args = parser.parse_args()

    target_date = date.fromisoformat(args.date) if args.date else date.today()
    results = asyncio.run(assess_experiments(target_date=target_date, dry_run=args.dry_run))

    if results:
        print(json.dumps(results, indent=2, default=str))


if __name__ == "__main__":
    main()
