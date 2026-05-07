"""Error classifier for run telemetry.

Pure-Python pattern matcher — no Ollama, no external dependencies.
Takes an error string and returns a structured dict with category,
retryability, corrective system, and a human-readable summary.
"""

import logging
import re

logger = logging.getLogger(__name__)

# Each rule: (category, compiled_pattern, retryable, corrective_system)
_RULES: list[tuple[str, re.Pattern, bool, str]] = [
    (
        "transient_api",
        re.compile(r"500|529|overloaded|internal server error|rate[_ ]limit", re.IGNORECASE),
        True,
        "retry_config",
    ),
    (
        "context_overflow",
        re.compile(r"context|too many tokens|max_tokens|token limit|context length", re.IGNORECASE),
        False,
        "session_trimming",
    ),
    (
        "timeout",
        re.compile(r"timeout|timed out|timeout exceeded", re.IGNORECASE),
        True,
        "retry_config",
    ),
    (
        "stuck_loop",
        re.compile(r"stuck in a loop|identical calls", re.IGNORECASE),
        False,
        "guardrails",
    ),
    (
        "budget_exceeded",
        re.compile(r"budget|denied by user|budget approval", re.IGNORECASE),
        False,
        "none",
    ),
]

_TOOL_FAILURE_START = re.compile(r"^Error:", re.IGNORECASE)
_TOOL_FAILURE_COMBO_A = re.compile(r"Tool.*failed|failed.*Tool", re.IGNORECASE)
_TOOL_FAILURE_COMBO_B = re.compile(r"tool_use.*error|error.*tool_use", re.IGNORECASE)


def _extract_summary(error_text: str, max_len: int = 120) -> str:
    """Extract the first meaningful sentence, capped at max_len chars."""
    # Split on sentence-ending punctuation or newlines
    parts = re.split(r"[.\n]", error_text.strip())
    for part in parts:
        cleaned = part.strip()
        if cleaned:
            if len(cleaned) > max_len:
                return cleaned[: max_len - 3] + "..."
            return cleaned
    # Fallback: truncate the raw text
    text = error_text.strip()
    if len(text) > max_len:
        return text[: max_len - 3] + "..."
    return text


def _is_tool_failure(error_text: str) -> bool:
    """Check the special tool_failure patterns that need multi-part matching."""
    if _TOOL_FAILURE_START.search(error_text):
        return True
    if _TOOL_FAILURE_COMBO_A.search(error_text):
        return True
    if _TOOL_FAILURE_COMBO_B.search(error_text):
        return True
    return False


def classify_error(error_text: str) -> dict:
    """Classify a run error into a category with structured metadata.

    Returns: {
        "category": str,       # one of the defined categories
        "retryable": bool,     # whether the run should be retried
        "corrective_system": str,  # which system should handle this
        "summary": str,        # one-line human-readable summary
    }
    """
    if not error_text or not error_text.strip():
        logger.debug("Empty error text received, classifying as unknown")
        return {
            "category": "unknown",
            "retryable": False,
            "corrective_system": "none",
            "summary": "(empty error)",
        }

    summary = _extract_summary(error_text)

    # Walk through the regex rules first — transient_api must take priority
    # over tool_failure since "Error code: 500" starts with "Error:" but
    # is an API issue, not a tool failure.
    for category, pattern, retryable, corrective_system in _RULES:
        if pattern.search(error_text):
            logger.debug("Classified error as %s: %s", category, summary)
            return {
                "category": category,
                "retryable": retryable,
                "corrective_system": corrective_system,
                "summary": summary,
            }

    # Tool failure check last — catches "Error: file not found" etc.
    if _is_tool_failure(error_text):
        logger.debug("Classified error as tool_failure: %s", summary)
        return {
            "category": "tool_failure",
            "retryable": False,
            "corrective_system": "skill_pitfalls",
            "summary": summary,
        }

    logger.debug("Classified error as unknown: %s", summary)
    return {
        "category": "unknown",
        "retryable": False,
        "corrective_system": "none",
        "summary": summary,
    }
