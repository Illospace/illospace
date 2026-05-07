"""Small task-analysis helpers used outside a specific recipe."""

from __future__ import annotations

import hashlib


def is_simple_task(task: str) -> bool:
    """Heuristic for whether a request is narrow enough for direct execution."""
    task_lower = str(task or "").lower()
    word_count = len(str(task or "").split())

    if word_count > 100:
        return False

    complex_signals = [
        "refactor",
        "redesign",
        "architect",
        "migrate",
        "investigate",
        "debug",
        "analyze",
        "complex",
        "multiple",
        "integrate",
        "pipeline",
        "system",
        "infrastructure",
    ]
    if any(signal in task_lower for signal in complex_signals):
        return False

    simple_signals = [
        "fix typo",
        "rename",
        "update version",
        "add comment",
        "change color",
        "update text",
        "run test",
        "format",
    ]
    if any(signal in task_lower for signal in simple_signals):
        return True

    return word_count < 30


def task_hash(task: str) -> str:
    """Stable semantic-ish hash for grouping similar task text."""
    words = sorted(set(word.lower() for word in str(task or "").split() if len(word) > 3))[:100]
    return hashlib.sha256(" ".join(words).encode()).hexdigest()[:16]


__all__ = ["is_simple_task", "task_hash"]
