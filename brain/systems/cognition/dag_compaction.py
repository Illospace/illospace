"""DAG compaction engine — depth-aware compression with three-level escalation.

Compresses groups of memories into summaries using LLM prompts tuned to
the DAG depth.  Falls back through three levels:

1. **Normal** — depth-aware prompt, standard model call.
2. **Aggressive** — tighter "durable facts only" prompt, on validation failure.
3. **Deterministic** — algorithmic line-scoring with no LLM call.

When callers do not pass a model, compaction uses the configured low-tier
model from :mod:`brain.platform.providers.model_policy`.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Sequence

from brain.platform.async_io import http_post

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Depth-aware prompts
# ---------------------------------------------------------------------------

DEPTH_PROMPTS: dict[int, str] = {
    0: (
        "What would a fresh agent need to continue this work tomorrow?\n"
        "Preserve: decisions made, rationale behind them, file paths, corrections applied."
    ),
    1: (
        "What's the arc? Goal, what happened, what carries forward.\n"
        "Preserve: goals, pivots, outcomes, unresolved questions."
    ),
    2: (
        "What would a fresh agent need to pick this up cold, weeks from now?\n"
        "Preserve: durable principles, project-level patterns."
    ),
}

AGGRESSIVE_PROMPT = (
    "Durable facts only. Remove all narrative, hedging, and speculation.\n"
    "Keep only: concrete decisions, file paths, names, version numbers, "
    "and lessons that would survive a month."
)

# Verbs that indicate decision/preference/outcome content
DECISION_VERBS = frozenset({
    "decided", "chose", "prefer", "selected", "adopted", "switched",
    "rejected", "confirmed", "resolved", "agreed", "committed",
    "use", "using", "picked", "settled",
})

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def get_depth_prompt(depth: int) -> str:
    """Return the compression prompt for a given DAG depth, capping at 2."""
    return DEPTH_PROMPTS[min(depth, 2)]


# ---------------------------------------------------------------------------
# Compression pipeline
# ---------------------------------------------------------------------------


def compress_memories(
    contents: list[str],
    source_ids: list[int],
    depth: int,
    model: str | None = None,
    *,
    user_id: str | None = None,
) -> dict:
    """Compress a batch of memory contents with three-level escalation.

    Args:
        contents: Raw memory content strings to compress.
        source_ids: Corresponding memory IDs (for breadcrumb attribution).
        depth: DAG depth of the resulting summary node.
        model: Optional model specifier (``provider/<model>`` or ``ollama:<model>``).

    Returns:
        ``{"content": str, "breadcrumbs": list[dict], "model_used": str, "level": str}``
    """
    if model is None:
        from brain.platform.providers.model_policy import get_model_for_tier

        model = get_model_for_tier("low", include_provider_prefix=True, user_id=user_id)

    combined_text = "\n---\n".join(contents)

    # --- Level 1: Normal ---
    depth_prompt = get_depth_prompt(depth)
    prompt = _build_prompt(depth_prompt, combined_text)
    raw = _call_model(prompt, model, user_id=user_id)

    if raw:
        content, breadcrumbs = _parse_compression_result(raw, source_ids)
        if validate_summary(content, contents, breadcrumbs):
            return {
                "content": content,
                "breadcrumbs": breadcrumbs,
                "model_used": model,
                "level": "normal",
            }
        logger.info("Normal compression failed validation — escalating to aggressive")

    # --- Level 2: Aggressive ---
    aggressive_prompt = _build_prompt(AGGRESSIVE_PROMPT, combined_text)
    raw = _call_model(aggressive_prompt, model, user_id=user_id)

    if raw:
        content, breadcrumbs = _parse_compression_result(raw, source_ids)
        if validate_summary(content, contents, breadcrumbs):
            return {
                "content": content,
                "breadcrumbs": breadcrumbs,
                "model_used": model,
                "level": "aggressive",
            }
        logger.info("Aggressive compression failed validation — falling back to deterministic")

    # --- Level 3: Deterministic fallback ---
    content = deterministic_fallback(contents)
    breadcrumbs = _build_fallback_breadcrumbs(contents, source_ids)
    return {
        "content": content,
        "breadcrumbs": breadcrumbs,
        "model_used": "deterministic",
        "level": "deterministic",
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_summary(
    content: str,
    sources: list[str],
    breadcrumbs: list[dict],
) -> bool:
    """Validate a compression result.

    Checks:
    1. Summary is shorter than combined sources.
    2. Contains at least one decision/preference/outcome verb.
    3. All breadcrumb topics are >= 3 chars.
    """
    if not content or not content.strip():
        return False

    combined_len = sum(len(s) for s in sources)
    if len(content) >= combined_len:
        return False

    # Check for at least one decision verb
    content_lower = content.lower()
    has_verb = any(verb in content_lower for verb in DECISION_VERBS)
    if not has_verb:
        return False

    # Breadcrumb topics should have reasonable length
    for bc in breadcrumbs:
        topic = bc.get("topic", "")
        if len(topic) < 3:
            return False

    return True


# ---------------------------------------------------------------------------
# Deterministic fallback
# ---------------------------------------------------------------------------


def deterministic_fallback(contents: list[str], max_tokens: int = 500) -> str:
    """Score-based line selection when LLM compression fails.

    Scoring:
    - +5 for lines containing decision verbs
    - +2 for first sentence of each content block
    - +1 for short lines (< 80 chars)

    Approximate token budget: 1 token ~ 4 chars.
    """
    scored_lines: list[tuple[float, str]] = []

    for block_idx, block in enumerate(contents):
        lines = block.strip().splitlines()
        for line_idx, line in enumerate(lines):
            line = line.strip()
            if not line:
                continue
            score = 0.0
            line_lower = line.lower()

            # Decision verb bonus
            if any(verb in line_lower for verb in DECISION_VERBS):
                score += 5.0

            # First sentence bonus
            if line_idx == 0:
                score += 2.0

            # Short line bonus
            if len(line) < 80:
                score += 1.0

            scored_lines.append((score, line))

    # Sort by score descending, take lines until budget
    scored_lines.sort(key=lambda x: x[0], reverse=True)

    char_budget = max_tokens * 4
    selected: list[str] = []
    used = 0

    for _score, line in scored_lines:
        if used + len(line) > char_budget:
            continue
        selected.append(line)
        used += len(line)

    return "\n".join(selected)


# ---------------------------------------------------------------------------
# Result parsing
# ---------------------------------------------------------------------------

_BREADCRUMB_RE = re.compile(
    r"Expand for details about:\s*\[([^\]]+)\]",
    re.IGNORECASE,
)


def _parse_compression_result(
    result: str,
    source_ids: list[int],
) -> tuple[str, list[dict]]:
    """Extract content and breadcrumbs from a model compression response.

    Looks for a line like ``Expand for details about: [topic1, topic2, ...]``
    and parses it into breadcrumbs with distributed source_ids.
    """
    match = _BREADCRUMB_RE.search(result)
    breadcrumbs: list[dict] = []

    if match:
        # Remove the breadcrumb line from content
        content = result[: match.start()].rstrip() + result[match.end() :].lstrip()
        topics = [t.strip() for t in match.group(1).split(",") if t.strip()]

        # Distribute source_ids across topics
        for i, topic in enumerate(topics):
            # Round-robin assign source IDs to topics
            assigned_ids = []
            if source_ids:
                # Each topic gets roughly equal share of source_ids
                start = (i * len(source_ids)) // max(len(topics), 1)
                end = ((i + 1) * len(source_ids)) // max(len(topics), 1)
                assigned_ids = source_ids[start:end] if start < end else [source_ids[i % len(source_ids)]]

            breadcrumbs.append({
                "topic": topic,
                "source_ids": assigned_ids,
            })
    else:
        content = result.strip()

    return content.strip(), breadcrumbs


def _build_fallback_breadcrumbs(
    contents: list[str],
    source_ids: list[int],
) -> list[dict]:
    """Build breadcrumbs for deterministic fallback — one per source."""
    breadcrumbs: list[dict] = []
    for i, (content, sid) in enumerate(zip(contents, source_ids)):
        # Use first 50 chars of content as topic
        topic = content[:50].strip().replace("\n", " ")
        if len(topic) < 3:
            topic = f"source-{sid}"
        breadcrumbs.append({
            "topic": topic,
            "source_ids": [sid],
        })
    return breadcrumbs


# ---------------------------------------------------------------------------
# Prompt building
# ---------------------------------------------------------------------------


def _build_prompt(instruction: str, combined_text: str) -> str:
    """Build the full compression prompt."""
    return (
        f"You are compressing a batch of memories into a concise summary.\n\n"
        f"{instruction}\n\n"
        f"At the end of your summary, include a line:\n"
        f"Expand for details about: [topic1, topic2, ...]\n"
        f"listing the key topics someone might want to drill into.\n\n"
        f"Memories:\n{combined_text}"
    )


# ---------------------------------------------------------------------------
# Model routing (same pattern as harvest.py)
# ---------------------------------------------------------------------------


def _call_model(prompt: str, model: str, *, user_id: str | None = None) -> str | None:
    """Route to local Ollama or the configured provider model."""
    try:
        if model.startswith("ollama:"):
            return _call_ollama(prompt, model.removeprefix("ollama:"))
        if model.startswith("claude:"):
            model = f"anthropic/{model.removeprefix('claude:')}"
        return _call_provider_model(prompt, model, user_id=user_id)
    except Exception:
        logger.exception("Model call failed for DAG compaction")
    return None


def _call_ollama(prompt: str, model: str) -> str | None:
    """Call Ollama for compression."""
    try:
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0.2, "num_predict": 600},
            "think": False,
            "keep_alive": "5m",
        }
        resp = http_post(
            f"{OLLAMA_URL}/api/generate",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        result = resp.json()
        return result.get("response", "").strip() or None
    except Exception as e:
        logger.warning("Ollama compaction call failed: %s", e)
        return None


def _call_provider_model(prompt: str, model: str, *, user_id: str | None = None) -> str | None:
    """Call the configured provider for compression."""
    try:
        from brain.platform.integrations.completions import simple_text_completion

        return simple_text_completion(
            prompt,
            model=model,
            max_tokens=800,
            user_id=user_id,
        )
    except Exception as e:
        logger.warning("Provider compaction call failed: %s", e)
    return None
