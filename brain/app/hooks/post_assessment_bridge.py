#!/usr/bin/env python3
"""
Post-Assessment Bridge for optional session integrations.

Thin bridge between message-sent integrations and self_assess.py.
Determines if a response warrants assessment, runs it, and outputs JSON.

Usage:
    python3 post_assessment_bridge.py '<message_content>'
    python3 post_assessment_bridge.py '{"task":"...", "outcome":"..."}'

Output: JSON with assessment result or {"skip": true} for trivial messages.
"""

import json
import re
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))

from brain.app.hooks.self_assess import assess_quality

# Messages shorter than this are trivial
MIN_CONTENT_LENGTH = 50

# Patterns that indicate non-substantive responses
SKIP_PATTERNS = [
    r"^NO_REPLY$",
    r"^\s*$",
    r"^[\U0001F600-\U0001FAFF\u2600-\u27BF\u200d\uFE0F\u2764]+$",  # emoji-only
]

_skip_re = [re.compile(p) for p in SKIP_PATTERNS]


def should_assess(content: str | None) -> bool:
    """Determine if a message warrants self-assessment."""
    if not content or not isinstance(content, str):
        return False

    stripped = content.strip()
    if len(stripped) < MIN_CONTENT_LENGTH:
        return False

    for pattern in _skip_re:
        if pattern.match(stripped):
            return False

    return True


def extract_task_context(content: str) -> dict:
    """Extract task and outcome from message content."""
    truncated = content[:1000]
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        payload = None

    if isinstance(payload, dict):
        task = str(payload.get("task") or payload.get("prompt") or payload.get("original_task") or "").strip()
        outcome = str(payload.get("outcome") or payload.get("response") or payload.get("content") or "").strip()
        if task or outcome:
            return {
                "task": (task or "agent response")[:1000],
                "outcome": (outcome or truncated)[:1000],
            }

    return {
        "task": truncated,
        "outcome": truncated,
    }


def run_assessment(task: str, outcome: str) -> dict | None:
    """Run self-assessment, returning result or None on error."""
    try:
        return assess_quality(task, outcome)
    except Exception as e:
        print(f"[post-assessment] Error: {e}", file=sys.stderr)
        return None


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(json.dumps({"skip": True, "reason": "no content"}))
        sys.exit(0)

    content = sys.argv[1]

    if not should_assess(content):
        print(json.dumps({"skip": True, "reason": "trivial"}))
        sys.exit(0)

    ctx = extract_task_context(content)
    result = run_assessment(ctx["task"], ctx["outcome"])

    if result is None:
        print(json.dumps({"skip": True, "reason": "error"}))
        sys.exit(0)

    print(json.dumps(result))
