#!/usr/bin/env python3
"""
Illo Brain — Output Validation & Anomaly Detection

Meta-layer that wraps every automated pipeline stage. Provides:
1. Output validation — does the output make structural sense?
2. Plausibility checks — is this conclusion reasonable?
3. Anomaly detection — are timings/sizes/counts within expected ranges?
4. Self-audit — review logs for silent failures
"""

import json
import os
import re
import sys
from datetime import datetime, date, timedelta
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))
import brain.kernel.config as config

from sqlalchemy import text

from brain.platform.db.repositories.unit_of_work import UnitOfWork, open_unit_of_work

PROJECT_ROOT = config.BRAIN_DIR
LOGS_DIR = config.BRAIN_LOG_DIR
PENDING_REFLECTION_PATH = config.PRIVATE_HOME / "PENDING_REFLECTION.json"

# Thresholds
NIGHTLY_MIN_DURATION_SEC = 30
CURIOSITY_MIN_CONTENT_LEN = 500
CURIOSITY_HTML_TAG_THRESHOLD = 0.01
MEMORY_MAX_PER_DAY = 100
REFLECTION_MIN_OUTPUT_KEYS = 5


def validate_nightly_log(target_date: date) -> tuple[bool, list[str]]:
    """Validate that a nightly cycle ran properly."""
    issues = []
    log_path = LOGS_DIR / f"nightly-{target_date}.log"

    if not log_path.exists():
        return False, [f"No nightly log found for {target_date}"]

    content = log_path.read_text()

    if "SLEEP CYCLE COMPLETE" not in content:
        issues.append("Nightly cycle did not complete")

    # Check for errors in each phase
    error_patterns = ["Traceback", "NameError", "TypeError", "AttributeError",
                      "ImportError", "KeyError", "FAILED"]
    for pattern in error_patterns:
        if pattern in content:
            for line in content.split('\n'):
                if pattern in line:
                    issues.append(f"Error in log: {line.strip()[:120]}")
                    break

    # Check phase presence
    for phase in ["PHASE 1", "PHASE 2", "PHASE 3", "PHASE 4", "PHASE 5", "PHASE 6"]:
        if phase not in content:
            issues.append(f"Phase missing from log: {phase}")

    # Check reflection output
    reflect_output = LOGS_DIR / f"reflect-output-{target_date}.json"
    pending = PENDING_REFLECTION_PATH
    if not reflect_output.exists() and not pending.exists():
        issues.append("No reflection output and no pending flag")

    return len(issues) == 0, issues


def validate_curiosity_output(target_date: date) -> tuple[bool, list[str]]:
    """Validate curiosity run produced meaningful results."""
    issues = []
    output_path = LOGS_DIR / f"curiosity-output-{target_date}.json"
    if not output_path.exists():
        return False, [f"No curiosity output for {target_date}"]

    try:
        with open(output_path) as f:
            reading = json.load(f)
    except json.JSONDecodeError:
        return False, ["Curiosity output is not valid JSON"]

    if reading.get("nothing_recent"):
        issues.append("Source reported nothing recent — verify content was fetched properly")

    return len(issues) == 0, issues


def validate_content_not_html(content: str) -> tuple[bool, list[str]]:
    """Check if content is readable text vs raw HTML."""
    issues = []
    html_density = content.count('<') / max(len(content), 1)
    if html_density > CURIOSITY_HTML_TAG_THRESHOLD:
        issues.append(f"Content appears to be raw HTML (tag density: {html_density:.3f})")

    html_artifacts = ['<!DOCTYPE', '<html', '<head', '<script', 'class="']
    artifact_count = sum(1 for a in html_artifacts if a in content[:2000])
    if artifact_count >= 2:
        issues.append(f"Found {artifact_count} HTML artifacts in first 2KB")

    return len(issues) == 0, issues


def validate_sub_agent_output(output: str, expected_format: str = "json") -> tuple[bool, list[str]]:
    """Validate child agent output before accepting it."""
    issues = []
    if not output or not output.strip():
        return False, ["Child agent returned empty output"]

    if expected_format == "json":
        try:
            data = json.loads(output)
            if isinstance(data, dict) and len(data) == 0:
                issues.append("Child agent returned empty JSON object")
        except json.JSONDecodeError:
            json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', output, re.DOTALL)
            if json_match:
                issues.append("JSON wrapped in markdown code fence — extraction needed")
            else:
                issues.append("Output is not valid JSON")

    return len(issues) == 0, issues


def validate_memory_count(target_date: date) -> tuple[bool, list[str]]:
    """Check memory creation bounds and duplicates."""
    issues = []
    try:
        with open_unit_of_work(UnitOfWork) as uow:
            row = uow.session.execute(text(
                "SELECT COUNT(*) as cnt FROM memories WHERE created_at::date = :dt AND NOT archived"
            ), {"dt": target_date}).mappings().first()
            count = row["cnt"]
            if count > MEMORY_MAX_PER_DAY:
                issues.append(f"{count} memories on {target_date} (threshold: {MEMORY_MAX_PER_DAY})")

            dupes = uow.session.execute(text("""
                SELECT m1.id, m2.id, 1 - (m1.semantic_embedding <=> m2.semantic_embedding) as similarity
                FROM memories m1 JOIN memories m2 ON m1.id < m2.id
                WHERE m1.created_at::date = :dt AND m2.created_at::date = :dt
                AND NOT m1.archived AND NOT m2.archived
                AND m1.semantic_embedding IS NOT NULL AND m2.semantic_embedding IS NOT NULL
                AND 1 - (m1.semantic_embedding <=> m2.semantic_embedding) > 0.85
                LIMIT 20
            """), {"dt": target_date}).all()
            if dupes:
                issues.append(f"{len(dupes)} near-duplicate pairs (>0.85 similarity)")
    except Exception as e:
        issues.append(f"Could not check memory count: {e}")

    return len(issues) == 0, issues


def audit_last_night(target_date: date | None = None) -> dict:
    """Full audit of last night's automated runs."""
    if target_date is None:
        target_date = (datetime.now() - timedelta(days=1)).date() \
            if datetime.now().hour < 12 else datetime.now().date()

    report = {
        "date": target_date.isoformat(),
        "timestamp": datetime.now().isoformat(),
        "checks": {},
        "all_passed": True,
        "total_issues": 0,
        "critical_issues": [],
    }

    for check_name, check_fn in [
        ("nightly_cycle", lambda: validate_nightly_log(target_date)),
        ("curiosity", lambda: validate_curiosity_output(target_date)),
        ("memory_health", lambda: validate_memory_count(target_date)),
    ]:
        passed, issues = check_fn()
        report["checks"][check_name] = {"passed": passed, "issues": issues}
        if not passed:
            report["all_passed"] = False
            report["total_issues"] += len(issues)
            for issue in issues:
                if any(kw in issue.lower() for kw in ["traceback", "error", "missing", "crash"]):
                    report["critical_issues"].append(f"[{check_name}] {issue}")

    return report


def format_audit_report(report: dict) -> str:
    """Format audit report as readable text."""
    lines = [f"🔍 Self-Audit — {report['date']}"]
    if report["all_passed"]:
        lines.append("✅ All checks passed.")
        return "\n".join(lines)

    lines.append(f"⚠ {report['total_issues']} issues found")
    for check_name, check in report["checks"].items():
        icon = "✅" if check["passed"] else "❌"
        lines.append(f"{icon} {check_name}")
        if not check["passed"]:
            for issue in check["issues"]:
                lines.append(f"    {issue}")
    return "\n".join(lines)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", help="Target date (YYYY-MM-DD)")
    args = parser.parse_args()
    target = date.fromisoformat(args.date) if args.date else None
    report = audit_last_night(target)
    print(format_audit_report(report))
