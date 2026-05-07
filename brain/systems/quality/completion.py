#!/usr/bin/env python3
"""Completion reviewer: auto-review child agent output against original ask.

Uses the existing review_gate.py to assess quality and generates
follow-up prompts when gaps are found.

Closes #70 (Structural Learning — Auto-Review on Completion).
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))

from brain.systems.quality.review import review_output


def review_completion(original_ask: str, output: str, files_changed: list[str] | None = None) -> dict:
    """Review child agent completion against the original ask.

    Args:
        original_ask: The original user request / task description.
        output: The child agent's output/report.
        files_changed: Optional list of files the child agent modified.

    Returns:
        dict with keys:
            - passed (bool): Whether the output meets quality bar
            - score (float): Quality score 0-1
            - concerns (list[str]): Issues found
            - gap_analysis (str): Summary of gaps
            - follow_up_prompt (str|None): Suggested prompt to close gaps (if failed)
    """
    result = review_output(task=original_ask, output=output, files_changed=files_changed)

    gap_analysis = ""
    follow_up_prompt = None

    if result.concerns:
        gap_analysis = "Issues found:\n" + "\n".join(f"- {c}" for c in result.concerns)

    if not result.passed:
        follow_up_prompt = _build_follow_up(original_ask, output, result.concerns)

    return {
        "passed": result.passed,
        "score": result.score,
        "concerns": result.concerns,
        "gap_analysis": gap_analysis,
        "follow_up_prompt": follow_up_prompt,
    }


def _build_follow_up(original_ask: str, output: str, concerns: list[str]) -> str:
    """Build a follow-up prompt to address gaps."""
    concern_text = "\n".join(f"- {c}" for c in concerns)
    return (
        f"The previous attempt did not fully satisfy the original request.\n\n"
        f"## Original Request\n> {original_ask}\n\n"
        f"## Issues Found\n{concern_text}\n\n"
        f"## Instructions\n"
        f"Please address each issue above. Specifically:\n"
        f"1. Re-read the original request carefully\n"
        f"2. Fix each concern listed\n"
        f"3. Verify your output matches the acceptance criteria\n"
        f"4. Include test evidence where applicable\n"
    )
