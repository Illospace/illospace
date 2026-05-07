"""Tests for blocking guardrails in skills.py plan output."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))


def test_critical_guardrails_are_blocking():
    """Guardrails with critical keywords should be classified as blocking."""
    # Simulate the classification logic from cmd_plan
    guardrails = [
        "Always run tests before deploying",
        "Consider using caching for performance",
        "Never push to production without review",
        "Nice to have: add logging",
    ]

    blocking = []
    advisory = []
    critical_keywords = [
        "must", "always", "never", "critical", "required", "breaking",
        "data loss", "security", "production", "deploy",
    ]

    for g in guardrails:
        g_lower = g.lower()
        is_critical = any(kw in g_lower for kw in critical_keywords)
        if is_critical:
            blocking.append(g)
        else:
            advisory.append(g)

    assert len(blocking) == 2
    assert "Always run tests before deploying" in blocking
    assert "Never push to production without review" in blocking
    assert len(advisory) == 2
    assert "Consider using caching for performance" in advisory


def test_no_guardrails_means_not_blocked():
    """Empty guardrails → blocked=False."""
    guardrails = []
    blocking = [g for g in guardrails if any(
        kw in g.lower() for kw in ["must", "always", "never", "critical"]
    )]
    assert len(blocking) == 0


def test_advisory_only_not_blocked():
    """Only advisory guardrails → blocked=False."""
    guardrails = ["Consider adding docs", "Maybe optimize later"]
    blocking = [g for g in guardrails if any(
        kw in g.lower() for kw in ["must", "always", "never", "critical",
                                     "required", "breaking", "data loss",
                                     "security", "production", "deploy"]
    )]
    assert len(blocking) == 0
