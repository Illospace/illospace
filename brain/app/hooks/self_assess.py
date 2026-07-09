#!/usr/bin/env python3
"""
Self-Assessment — Post-task quality check before presenting work to the user.

Queries the brain for similar past tasks and their outcomes, checks against
the pre-flight checklist, and returns a quality assessment.

The checklist has two axes. The *work-mode* axis (code/investigation/delegation)
selects an engineering quality bar. The *domain* axis
(``brain/systems/task_domain.py``) recognizes non-engineering work
(business/product/ops) and overrides that bar with a domain-appropriate one —
so a launch plan is not graded with "run tests and paste output". Engineering
and ambiguous tasks keep the work-mode bar; only positive non-engineering
evidence overrides it.

Usage:
    python3 self_assess.py "task description" "outcome summary"

Set SELF_ASSESS_NO_BRAIN=1 to skip brain queries (for testing/offline use).
"""

import json
import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))

from brain.systems.task_domain import TaskDomain, classify_task_domain

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


# Domain-specific checklists. These OVERRIDE the work-mode checklist only when we
# have positive evidence the task is non-engineering, so engineering and
# ambiguous tasks keep their existing work-mode bar (see brain/systems/task_domain.py).
_DOMAIN_CHECKLISTS = {
    TaskDomain.BUSINESS: [
        "State the objective and who owns the outcome",
        "Name the deadline or decision date",
        "Define what 'done' looks like — the concrete deliverable",
        "Identify who needs to be informed or sign off",
    ],
    TaskDomain.PRODUCT: [
        "State the user problem and the success metric",
        "Record the decision and its rationale",
        "List affected surfaces/users and dependencies",
        "Note what is explicitly out of scope",
    ],
    TaskDomain.OPS: [
        "State the action and its blast radius",
        "Confirm the rollback / recovery path",
        "Verify access and prerequisites before acting",
        "Record what changed for the next on-call",
    ],
}


def select_checklist(domain: TaskDomain, work_mode: str) -> tuple[list[str], str]:
    """Return ``(checklist, label)``.

    A non-engineering domain (business/product/ops) overrides the work-mode
    checklist so the quality bar fits the work. Engineering and ambiguous
    (``OTHER``) tasks keep their existing work-mode checklist — we only override
    on positive non-engineering evidence.
    """
    if domain in _DOMAIN_CHECKLISTS:
        return _DOMAIN_CHECKLISTS[domain] + _BASE_CHECKLIST, domain.value
    return get_checklist(work_mode), work_mode


def get_brain_context(query: str) -> dict:
    if os.environ.get("SELF_ASSESS_NO_BRAIN"):
        return {"memories": [], "warnings": [], "guardrails": []}
    try:
        from brain.app.hooks.brain_context import get_context
        return get_context(query)
    except Exception as e:
        return {"memories": [], "warnings": [], "guardrails": [], "error": str(e)}


def assess_quality(task_description: str, outcome_summary: str) -> dict:
    work_mode = classify_task_type(task_description)
    domain = classify_task_domain(task_description)
    checklist, checklist_label = select_checklist(domain, work_mode)
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
        "task_domain": domain.value,
        # Work-mode axis, kept under the historical "task_type" key for continuity.
        "task_type": work_mode,
        "checklist_label": checklist_label,
        "checklist": checklist,
        "warnings": warnings,
        "relevant_lessons": relevant_lessons,
        "assessment": assessment,
    }


def format_assessment(result: dict) -> str:
    domain = result.get("task_domain", "?")
    label = result.get("checklist_label", result.get("task_type", "?"))
    header = f"[Self-Assessment] Domain: {domain}"
    if result.get("task_type") and domain in ("engineering", "other"):
        header += f" · Mode: {result['task_type']}"
    header += f" | {result['assessment']}"
    parts = [header]
    if result["warnings"]:
        parts.append("\n⚠️ WARNINGS:")
        for w in result["warnings"]:
            parts.append(f"  • {w}")
    if result["relevant_lessons"]:
        parts.append("\n🧠 RELEVANT LESSONS:")
        for l in result["relevant_lessons"]:
            parts.append(f"  • {l[:150]}")
    parts.append(f"\n📋 CHECKLIST ({label}):")
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
