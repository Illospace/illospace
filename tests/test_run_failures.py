from __future__ import annotations

import pytest

from brain.systems.runs.failures import terminal_run_notice_condition


@pytest.mark.parametrize(
    ("status", "category", "expected"),
    [
        ("failed", "internal", "terminal:failed:internal"),
        ("failed", "upstream", "terminal:failed:upstream"),
        ("failed", "verification", "terminal:failed:verification"),
        ("canceled", None, "terminal:canceled:internal"),
        ("expired", None, "terminal:expired:internal"),
        ("completed", None, None),
    ],
)
def test_terminal_run_notice_condition_is_stable_and_distinguishes_escalations(
    status,
    category,
    expected,
):
    assert terminal_run_notice_condition(status, category) == expected
