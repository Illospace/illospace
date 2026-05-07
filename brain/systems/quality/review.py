#!/usr/bin/env python3
"""Child agent output review gate.

Automatically assesses child agent output for quality before acceptance.
Checks: task-output alignment, phantom tests, DRY violations, brain context.

Usage:
    python3 review_gate.py --task "description" --output "child agent report" --files "file1,file2"
"""

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))

from brain.app.hooks.brain_context import get_context


@dataclass
class ReviewResult:
    passed: bool
    concerns: list[str] = field(default_factory=list)
    score: float = 0.5
    brain_context_used: bool = False


# --- Detection patterns ---

_TEST_CLAIM_PATTERNS = re.compile(
    r"tests?\s+pass|tests?\s+passing|test\s+suite\s+pass|all\s+tests?\s+green",
    re.IGNORECASE,
)

_INVESTIGATION_KEYWORDS = {"investigate", "research", "analyze", "find out", "look into", "diagnose", "understand why"}

_IMPLEMENTATION_SIGNALS = {"fixed", "implemented", "added", "refactored", "built", "created", "shipped"}


def _check_phantom_tests(output: str, files_changed: list[str]) -> str | None:
    """Flag if output claims tests pass but no test files in changed files."""
    if not _TEST_CLAIM_PATTERNS.search(output):
        return None
    has_test_file = any("test" in f.lower() for f in files_changed)
    if not has_test_file:
        return "Phantom tests: output claims tests pass but no test file in changed files"
    return None


def _check_task_output_mismatch(task: str, output: str, files_changed: list[str]) -> str | None:
    """Flag if task type doesn't match output type."""
    task_lower = task.lower()
    output_lower = output.lower()

    is_investigation_task = any(kw in task_lower for kw in _INVESTIGATION_KEYWORDS)
    if not is_investigation_task:
        return None

    # Check if output is implementation rather than findings
    impl_signals = sum(1 for s in _IMPLEMENTATION_SIGNALS if s in output_lower)
    finding_signals = sum(1 for kw in ["found", "root cause", "because", "recommendation", "analysis", "instances", "lines"] if kw in output_lower)

    if impl_signals > finding_signals and len(files_changed) > 0:
        return "Task/output mismatch: task requested investigation but output appears to be implementation"
    return None


def _check_dry_violations(files_changed: list[str]) -> str | None:
    """Flag suspiciously many files changed (potential copy-paste)."""
    if len(files_changed) > 10:
        return f"DRY concern: {len(files_changed)} files changed — review for copy-paste patterns"
    return None


def _check_output_depth(task: str, output: str) -> str | None:
    """Flag superficial output for non-trivial tasks."""
    if len(output.split()) < 10 and len(task.split()) > 3:
        return "Shallow output: response is very brief for a non-trivial task"
    return None


def _compute_score(concerns: list[str], output: str, files_changed: list[str]) -> float:
    """Compute a 0-1 quality score."""
    score = 1.0
    # Deduct for each concern
    score -= len(concerns) * 0.25
    # Bonus for detailed output
    word_count = len(output.split())
    if word_count > 30:
        score += 0.1
    elif word_count < 10:
        score -= 0.2
    return max(0.0, min(1.0, round(score, 2)))


def review_output(
    task: str,
    output: str,
    files_changed: list[str] | None = None,
) -> ReviewResult:
    """Review child agent output for quality issues.

    Args:
        task: The original task description given to the child agent.
        output: The child agent's output/report.
        files_changed: List of file paths the child agent modified.

    Returns:
        ReviewResult with pass/fail, concerns list, and quality score.
    """
    files_changed = files_changed or []
    concerns = []
    brain_used = False

    # 1. Query brain for relevant context
    try:
        ctx = get_context(task)
        brain_used = True
        # Incorporate guardrail warnings as additional checks
        for g in ctx.get("guardrails", []):
            failure_text = g.get("failure", "").lower()
            # If brain has a relevant recent failure, flag it
            if "test" in failure_text and _TEST_CLAIM_PATTERNS.search(output):
                concerns.append(f"Brain guardrail: recent failure on '{g['skill']}' — {g['failure']}")
        for w in ctx.get("warnings", []):
            if any(kw in w.lower() for kw in ["test", "verify", "check"]):
                # Reinforce brain warnings in review
                if _TEST_CLAIM_PATTERNS.search(output) and not any("test" in f.lower() for f in files_changed):
                    concerns.append(f"Brain warning reinforces: {w[:100]}")
    except Exception:
        pass  # Brain unavailable — continue with heuristic checks

    # 2. Heuristic checks
    checks = [
        _check_phantom_tests(output, files_changed),
        _check_task_output_mismatch(task, output, files_changed),
        _check_dry_violations(files_changed),
        _check_output_depth(task, output),
    ]
    concerns.extend(c for c in checks if c is not None)

    score = _compute_score(concerns, output, files_changed)
    passed = score >= 0.5 and not any("mismatch" in c.lower() or "phantom" in c.lower() for c in concerns)

    return ReviewResult(
        passed=passed,
        concerns=concerns,
        score=score,
        brain_context_used=brain_used,
    )


# --- CLI ---

def main():
    parser = argparse.ArgumentParser(description="Child agent output review gate")
    parser.add_argument("--task", required=True, help="Original task description")
    parser.add_argument("--output", required=True, help="Child agent output/report")
    parser.add_argument("--files", default="", help="Comma-separated list of changed files")
    args = parser.parse_args()

    files = [f.strip() for f in args.files.split(",") if f.strip()]
    result = review_output(task=args.task, output=args.output, files_changed=files)

    print(json.dumps({
        "passed": result.passed,
        "score": result.score,
        "concerns": result.concerns,
        "brain.app.hooks.brain_context_used": result.brain_context_used,
    }, indent=2))
    sys.exit(0 if result.passed else 1)


if __name__ == "__main__":
    main()
