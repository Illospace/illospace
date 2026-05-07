#!/usr/bin/env python3
"""Change Confidence Assessor — self-honest routing for nightly changes.

Evaluates a proposed change and returns a confidence score + routing decision.
Used by nightly_implement.py to decide auto-merge vs PR-for-review.
"""

import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))

# Risk surface keywords by category
RISK_KEYWORDS = {
    "db_schema": ["CREATE TABLE", "ALTER TABLE", "DROP TABLE", "migration", "schema"],
    "api_endpoint": ["@app.route", "@router.", "endpoint", "api/v"],
    "auth": ["auth", "token", "password", "secret", "credential", "jwt", "oauth"],
    "external": ["requests.post", "requests.get", "urllib", "httpx", "aiohttp",
                 "smtp", "sendgrid", "twilio", "stripe", "webhook"],
}

# File patterns indicating high reversibility
HIGH_REVERSIBILITY_PATTERNS = [
    r"\.md$", r"\.json$", r"\.ya?ml$", r"\.toml$", r"\.cfg$", r"\.ini$",
    r"\.txt$", r"\.env",
]

# File patterns indicating low reversibility
LOW_REVERSIBILITY_PATTERNS = [
    r"migration", r"schema", r"alembic",
]

# Weights for each factor
WEIGHTS = {
    "scope": 0.25,
    "familiarity": 0.20,
    "test_coverage": 0.25,
    "reversibility": 0.15,
    "risk_surface": 0.15,
}

AUTO_MERGE_THRESHOLD = 0.7


def score_scope(files_changed: list[str], lines_changed: int) -> tuple[float, str]:
    """Score based on scope of changes. Fewer files/lines = higher confidence."""
    n_files = len(files_changed)
    reasoning_parts = []

    if n_files == 0:
        return 1.0, "No files changed"

    # File count scoring
    if n_files == 1:
        file_score = 1.0
    elif n_files <= 3:
        file_score = 0.8
    elif n_files <= 5:
        file_score = 0.6
    elif n_files <= 10:
        file_score = 0.4
    else:
        file_score = 0.2
    reasoning_parts.append(f"{n_files} file(s) changed → {file_score:.1f}")

    # Line count scoring
    if lines_changed <= 10:
        line_score = 1.0
    elif lines_changed <= 50:
        line_score = 0.8
    elif lines_changed <= 150:
        line_score = 0.6
    elif lines_changed <= 500:
        line_score = 0.4
    else:
        line_score = 0.2
    reasoning_parts.append(f"{lines_changed} lines changed → {line_score:.1f}")

    score = (file_score + line_score) / 2
    return score, "; ".join(reasoning_parts)


def score_familiarity(files_changed: list[str], query_brain_fn=None) -> tuple[float, str]:
    """Score based on whether similar changes have succeeded before.

    query_brain_fn: optional callable(query_str) -> list[dict] with 'content' keys
    """
    if not query_brain_fn:
        return 0.5, "No brain query available — defaulting to 0.5"

    try:
        # Query for similar past successful changes
        query = f"successful changes to {', '.join(files_changed[:3])}"
        results = query_brain_fn(query)
        if not results:
            return 0.3, "No similar past changes found in brain"

        # More precedent = higher confidence
        n = len(results)
        if n >= 5:
            score = 0.9
        elif n >= 3:
            score = 0.7
        elif n >= 1:
            score = 0.5
        else:
            score = 0.3
        return score, f"Found {n} similar past change(s) in brain"
    except Exception as e:
        return 0.4, f"Brain query failed: {e}"


def score_test_coverage(tests_passed: bool, tests_exist: bool,
                        new_tests_written: bool) -> tuple[float, str]:
    """Score based on test coverage and results."""
    reasoning_parts = []

    if not tests_exist:
        reasoning_parts.append("No tests exist for changed code")
        return 0.2, "; ".join(reasoning_parts)

    score = 0.4  # Base: tests exist
    reasoning_parts.append("Tests exist")

    if tests_passed:
        score += 0.3
        reasoning_parts.append("all passing")
    else:
        score = 0.1  # Override: failing tests are a strong signal
        reasoning_parts.append("TESTS FAILING")
        return score, "; ".join(reasoning_parts)

    if new_tests_written:
        score += 0.3
        reasoning_parts.append("new tests written")

    return min(score, 1.0), "; ".join(reasoning_parts)


def score_reversibility(files_changed: list[str], is_new_file: bool = False,
                        diff_description: str = "") -> tuple[float, str]:
    """Score based on how easily the change can be reverted."""
    if not files_changed:
        return 1.0, "No files to revert"

    if is_new_file and len(files_changed) == 1:
        return 0.9, "New file — easy to delete if needed"

    scores = []
    reasons = []

    for f in files_changed:
        # Check high reversibility patterns (config, docs)
        if any(re.search(p, f) for p in HIGH_REVERSIBILITY_PATTERNS):
            scores.append(0.9)
            reasons.append(f"{f}: config/doc file")
            continue

        # Check low reversibility patterns (migrations, schemas)
        if any(re.search(p, f) for p in LOW_REVERSIBILITY_PATTERNS):
            scores.append(0.2)
            reasons.append(f"{f}: migration/schema — hard to revert")
            continue

        # Default: moderate
        scores.append(0.6)
        reasons.append(f"{f}: code file — moderate reversibility")

    avg = sum(scores) / len(scores) if scores else 0.5
    return avg, "; ".join(reasons[:3])  # Cap reasons at 3


def score_risk_surface(files_changed: list[str],
                       diff_content: str = "") -> tuple[float, str]:
    """Score based on whether changes touch risky areas."""
    risks_found = []

    content_to_check = diff_content.lower()
    for category, keywords in RISK_KEYWORDS.items():
        for kw in keywords:
            if kw.lower() in content_to_check:
                risks_found.append(category)
                break

    # Also check file paths
    for f in files_changed:
        fl = f.lower()
        if "auth" in fl or "secret" in fl:
            risks_found.append("auth")
        if "migration" in fl or "schema" in fl:
            risks_found.append("db_schema")
        if "api" in fl or "endpoint" in fl:
            risks_found.append("api_endpoint")

    risks_found = list(set(risks_found))

    if not risks_found:
        return 0.9, "No risky surface areas detected"

    # More risk categories = lower score
    n = len(risks_found)
    if n >= 3:
        score = 0.2
    elif n == 2:
        score = 0.4
    else:
        score = 0.6

    return score, f"Risk areas: {', '.join(risks_found)}"


def assess_confidence(
    files_changed: list[str],
    lines_changed: int = 0,
    diff_content: str = "",
    diff_description: str = "",
    tests_passed: bool = True,
    tests_exist: bool = True,
    new_tests_written: bool = False,
    is_new_file: bool = False,
    query_brain_fn=None,
    identified_risks: list[str] | None = None,
) -> dict:
    """Assess confidence in a proposed change and return routing decision.

    Returns:
        {
            "confidence": float 0.0-1.0,
            "route": "auto_merge" | "pr_for_review",
            "reasoning": str,
            "factors": {name: {"score": float, "reasoning": str}},
        }
    """
    factors = {}

    # Score each factor
    s, r = score_scope(files_changed, lines_changed)
    factors["scope"] = {"score": s, "weight": WEIGHTS["scope"], "reasoning": r}

    s, r = score_familiarity(files_changed, query_brain_fn)
    factors["familiarity"] = {"score": s, "weight": WEIGHTS["familiarity"], "reasoning": r}

    s, r = score_test_coverage(tests_passed, tests_exist, new_tests_written)
    factors["test_coverage"] = {"score": s, "weight": WEIGHTS["test_coverage"], "reasoning": r}

    s, r = score_reversibility(files_changed, is_new_file, diff_description)
    factors["reversibility"] = {"score": s, "weight": WEIGHTS["reversibility"], "reasoning": r}

    s, r = score_risk_surface(files_changed, diff_content)
    factors["risk_surface"] = {"score": s, "weight": WEIGHTS["risk_surface"], "reasoning": r}

    # Weighted average
    confidence = sum(
        factors[k]["score"] * factors[k]["weight"] for k in WEIGHTS
    )

    # Self-honesty: if no risks identified, that's a yellow flag
    honesty_penalty = 0.0
    if identified_risks is not None and len(identified_risks) == 0:
        honesty_penalty = 0.1
        confidence = max(0.0, confidence - honesty_penalty)

    # Build reasoning
    reasoning_parts = []
    for name, data in factors.items():
        reasoning_parts.append(f"  {name} ({data['weight']:.0%}): {data['score']:.2f} — {data['reasoning']}")

    # What could go wrong section
    risk_section = []
    if identified_risks:
        risk_section = identified_risks
    else:
        # Auto-generate risk observations
        if factors["risk_surface"]["score"] < 0.7:
            risk_section.append(f"Touches risk areas: {factors['risk_surface']['reasoning']}")
        if factors["scope"]["score"] < 0.6:
            risk_section.append("Large scope increases chance of unintended side effects")
        if factors["test_coverage"]["score"] < 0.5:
            risk_section.append("Low test coverage — bugs may go undetected")
        if not risk_section:
            risk_section.append("No specific risks identified (honesty penalty applied: -0.1)")

    reasoning = (
        f"Confidence: {confidence:.2f}\n"
        f"Factor breakdown:\n" + "\n".join(reasoning_parts) + "\n"
        + (f"Honesty penalty: -{honesty_penalty}\n" if honesty_penalty else "")
        + "What could go wrong:\n"
        + "\n".join(f"  - {r}" for r in risk_section)
    )

    # Routing decision
    if confidence >= AUTO_MERGE_THRESHOLD and tests_passed:
        route = "auto_merge"
    else:
        route = "pr_for_review"

    return {
        "confidence": round(confidence, 3),
        "route": route,
        "reasoning": reasoning,
        "factors": factors,
    }
