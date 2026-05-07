"""Tests for skills.py — pure functions and maturity computation."""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 1))))

from brain.app.cli.skills import compute_maturity


def test_maturity_emerging():
    """< 3 uses → emerging."""
    maturity, confidence = compute_maturity(0, 0.0)
    assert maturity == "emerging"
    assert confidence <= 0.3

    maturity, confidence = compute_maturity(2, 1.0)
    assert maturity == "emerging"


def test_maturity_developing():
    """3-9 uses → developing."""
    maturity, confidence = compute_maturity(5, 0.6)
    assert maturity == "developing"
    assert 0.0 < confidence <= 0.6


def test_maturity_proficient():
    """10-24 uses with >= 70% success → proficient."""
    maturity, confidence = compute_maturity(15, 0.8)
    assert maturity == "proficient"
    assert confidence <= 0.85


def test_maturity_expert():
    """25+ uses with >= 85% success → expert."""
    maturity, confidence = compute_maturity(30, 0.9)
    assert maturity == "expert"
    assert confidence <= 1.0


def test_maturity_developing_low_success():
    """High use count but low success → developing."""
    maturity, confidence = compute_maturity(30, 0.5)
    assert maturity == "developing"


def test_maturity_confidence_bounds():
    """Confidence should always be in [0, 1]."""
    for use in [0, 1, 5, 10, 25, 50, 100]:
        for rate in [0.0, 0.5, 0.8, 1.0]:
            _, confidence = compute_maturity(use, rate)
            assert 0.0 <= confidence <= 1.0, f"Out of bounds: use={use}, rate={rate}, conf={confidence}"
