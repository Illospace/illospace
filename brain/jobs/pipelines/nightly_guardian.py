#!/usr/bin/env python3
"""Nightly Guardian Audit — review the day's completions and generate new rules.

Reviews all skill executions from the day, checks for ignored guardrails,
and generates new guardian rules from recurring patterns.

Usage:
    python3 -m brain.jobs.pipelines.nightly_guardian [--date 2026-03-04]
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import date

from sqlalchemy import text

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))

from brain.platform.db.repositories.unit_of_work import UnitOfWork


async def audit_day(target_date: str | None = None):
    """Review all skill executions and violations for the given day."""
    target = target_date or date.today().isoformat()
    print(f"[guardian-audit] Auditing {target}")

    async with UnitOfWork() as uow:
        # 1. Get all skill executions from the day
        result = await uow.session.execute(text("""
            SELECT se.id, se.task_description, se.outcome, s.name as skill_name,
                   se.outcome_details, se.started_at
            FROM skill_executions se
            JOIN skills s ON s.id = se.skill_id
            WHERE se.started_at::date = CAST(:target AS date)
            ORDER BY se.started_at
        """), {"target": target})
        executions = result.mappings().all()

        print(f"[guardian-audit] Found {len(executions)} executions")

        # 2. Get violations from the day
        result = await uow.session.execute(text("""
            SELECT detected_by, context, session_date
            FROM violation_log
            WHERE session_date = CAST(:target AS date)
        """), {"target": target})
        violations = result.mappings().all()

        print(f"[guardian-audit] Found {len(violations)} violations")

        # 3. Analyze patterns — recurring violation types
        result = await uow.session.execute(text("""
            SELECT context, COUNT(*) as cnt
            FROM violation_log
            WHERE session_date >= CAST(:target AS date) - INTERVAL '7 days'
            GROUP BY context
            HAVING COUNT(*) >= 3
            ORDER BY cnt DESC
        """), {"target": target})
        recurring = result.mappings().all()

        # 4. Report recurring patterns.
        for pattern in recurring:
            ctx = pattern["context"]
            cnt = pattern["cnt"]
            print(f"[guardian-audit] Recurring pattern ({cnt}x this week): {ctx[:80]}")

        # 5. Check for executions with failures that had no violations logged
        failure_count = sum(1 for e in executions if e["outcome"] == "failure")
        violation_count = len(violations)
        if failure_count > violation_count:
            gap = failure_count - violation_count
            print(f"[guardian-audit] WARNING: {gap} failures had no guardian violations — guardrails may be too lax")

        # 6. Summary
        summary = {
            "date": target,
            "total_executions": len(executions),
            "failures": failure_count,
            "violations_logged": violation_count,
            "recurring_patterns": len(recurring),
            "new_rules_created": 0,
        }
        print(f"[guardian-audit] Summary: {json.dumps(summary)}")
        return summary


def main():
    parser = argparse.ArgumentParser(description="Nightly Guardian Audit")
    parser.add_argument("--date", help="Date to audit (YYYY-MM-DD)")
    args = parser.parse_args()
    asyncio.run(audit_day(args.date))


if __name__ == "__main__":
    main()
