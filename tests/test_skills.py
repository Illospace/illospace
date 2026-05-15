"""Tests for skills.py — pure functions and maturity computation."""

import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))

from brain.app.cli.skills import compute_maturity


@pytest.mark.parametrize(
    ("use_count", "success_rate", "expected_maturity", "min_confidence", "max_confidence"),
    [
        (0, 0.0, "emerging", None, 0.3),
        (2, 1.0, "emerging", None, None),
        (5, 0.6, "developing", 0.0, 0.6),
        (15, 0.8, "proficient", None, 0.85),
        (30, 0.9, "expert", None, 1.0),
        (30, 0.5, "developing", None, None),
    ],
)
def test_maturity_thresholds(use_count, success_rate, expected_maturity, min_confidence, max_confidence):
    maturity, confidence = compute_maturity(use_count, success_rate)
    assert maturity == expected_maturity
    if min_confidence is not None:
        assert confidence > min_confidence
    if max_confidence is not None:
        assert confidence <= max_confidence


def test_maturity_confidence_bounds():
    """Confidence should always be in [0, 1]."""
    for use in [0, 1, 5, 10, 25, 50, 100]:
        for rate in [0.0, 0.5, 0.8, 1.0]:
            _, confidence = compute_maturity(use, rate)
            assert 0.0 <= confidence <= 1.0, f"Out of bounds: use={use}, rate={rate}, conf={confidence}"
