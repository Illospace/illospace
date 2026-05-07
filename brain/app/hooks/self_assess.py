#!/usr/bin/env python3
"""
Self-Assessment — Post-task quality check before presenting work to the user.

Queries the brain for similar past tasks and their outcomes, checks against
the pre-flight checklist, and returns a quality assessment.

Usage:
    python3 self_assess.py "task description" "outcome summary"

Set SELF_ASSESS_NO_BRAIN=1 to skip brain queries (for testing/offline use).
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))

_TASK_PATTERNS = [
    ("delegation", r"(?i)\b(delegat\w*|child.?agent|spawn|orchestrat\w*|parallel|worker|assign)\b"),
    ("investigation", r"(?i)\b(investigat\w*|debug\w*|trac\w+|find.?out|check.?why|look.?into|diagnos\w*|analyz\w*|root.?cause|figure.?out|inspect\w*|audit\w*)\b"),
    ("code", r"(?i)\b(fix\w*|implement\w*|refactor\w*|build\w*|add|creat\w*|updat\w*|patch\w*|ship\w*|deploy\w*|endpoint|bug|tests?\b|code|api|function|class|module|merge|PR|write)\b"),
]


def classify_task_type(task: str) -> str:
    for ttype, pattern in _TASK_PATTERNS:
        if re.search(pattern, task):
            return ttype
    return "generic"


_BASE_CHECKLIST = [
    "Apply self-critique: is this plausible? Did I verify?",
    "Encode lessons learned to the brain",
]

_CHECKLISTS = {
    "code": [
        "Run tests and paste output — no test output = not done",
        "Trace every code path end-to-end before presenting as ready",
        "Verify all data assumptions with actual values, not memory",
        "Check edge cases and null/None paths",
        "Before completing DB-touching code, verify connection context managers",
        "Before completing data writes, grep for all API consumers of that field",
    ],
    "investigation": [
        "Show the data you queried — no data = speculation",
        "Print actual values at the failure point, don't assume",
        "Trace the real data path end-to-end and log actual values",
        "Root cause identified, not just symptoms?",
    ],
    "delegation": [
        "Validate child agent output — 'nothing found' from active source is suspicious",
        "Check if remaining run graph steps exist after child agent completion",
        "Child agent completion is a TRIGGER to continue, not an endpoint",
    ],
    "generic": [],
}


def get_checklist(task_type: str) -> list[str]:
    return _CHECKLISTS.get(task_type, []) + _BASE_CHECKLIST


def get_brain_context(query: str) -> dict:
    if os.environ.get("SELF_ASSESS_NO_BRAIN"):
        return {"memories": [], "warnings": [], "guardrails": []}
    try:
        from brain.app.hooks.brain_context import get_context
        return get_context(query)
    except Exception as e:
        return {"memories": [], "warnings": [], "guardrails": [], "error": str(e)}


def assess_quality(task_description: str, outcome_summary: str) -> dict:
    task_type = classify_task_type(task_description)
    checklist = get_checklist(task_type)
    combined = f"{task_description} {outcome_summary}".strip()

    brain = get_brain_context(combined) if combined else {"memories": [], "warnings": [], "guardrails": []}

    warnings = list(brain.get("warnings", []))
    for g in brain.get("guardrails", []):
        warnings.append(f"[{g.get('skill', '?')}] {g.get('failure', 'unknown')}")

    relevant_lessons = []
    for m in brain.get("memories", []):
        if m.get("type") in ("lesson", "pattern"):
            relevant_lessons.append(m["content"])

    assessment = "concerns" if warnings else "pass"

    return {
        "task_type": task_type,
        "checklist": checklist,
        "warnings": warnings,
        "relevant_lessons": relevant_lessons,
        "assessment": assessment,
    }


def format_assessment(result: dict) -> str:
    parts = [f"[Self-Assessment] Task Type: {result['task_type']} | {result['assessment']}"]
    if result["warnings"]:
        parts.append("\n⚠️ WARNINGS:")
        for w in result["warnings"]:
            parts.append(f"  • {w}")
    if result["relevant_lessons"]:
        parts.append("\n🧠 RELEVANT LESSONS:")
        for l in result["relevant_lessons"]:
            parts.append(f"  • {l[:150]}")
    parts.append(f"\n📋 CHECKLIST ({result['task_type']}):")
    for item in result["checklist"]:
        parts.append(f"  ☐ {item}")
    return "\n".join(parts)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python3 self_assess.py 'task description' 'outcome summary'", file=sys.stderr)
        sys.exit(1)
    task, outcome = sys.argv[1], sys.argv[2]
    result = assess_quality(task, outcome)
    print(json.dumps(result))
    print("\n" + format_assessment(result))
