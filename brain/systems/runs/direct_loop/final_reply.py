"""Final-reply parsing and cache helpers for the agent loop."""

from __future__ import annotations

import json
from typing import Any


def extract_latest_user_intent(message: str) -> str:
    """Extract the latest user request from coordinator task wrappers when present."""
    text = (message or "").strip()
    marker = "Latest user message:"
    if marker in text:
        return text.split(marker, 1)[1].strip() or text
    return text


def parse_checker_payload(raw_output: str) -> dict | None:
    """Parse checker output as either compact tokens or structured JSON."""
    text = (raw_output or "").strip()
    if not text:
        return None

    upper = text.upper()
    if upper == "RESOLVED":
        return {
            "status": "resolved",
            "rationale": "Checker approved the final reply.",
            "missing_requirements": [],
        }
    if upper == "UNRESOLVED":
        return {
            "status": "continue",
            "rationale": "Checker marked the final reply unresolved.",
            "missing_requirements": [],
        }

    candidates = [text]
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace >= 0 and last_brace > first_brace:
        candidates.append(text[first_brace:last_brace + 1])

    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        status = str(payload.get("status") or payload.get("decision") or "").strip().lower()
        status_aliases = {
            "ship": "resolved",
            "done": "resolved",
            "ask_user": "blocked_on_user",
            "blocked": "blocked_on_user",
            "blocked_by_dependency": "blocked_on_user",
        }
        status = status_aliases.get(status, status)
        if status in {"resolved", "blocked_on_user", "continue"}:
            payload["status"] = status
            missing = payload.get("missing_requirements") or []
            if not isinstance(missing, list):
                missing = [str(missing)]
            payload["missing_requirements"] = [str(item)[:300] for item in missing]
            payload["rationale"] = str(payload.get("rationale") or "").strip()
            return payload
    return None


def normalize_final_reply_candidate(candidate_output: str) -> str:
    """Normalize candidate text so identical replies reuse the same checker verdict."""
    return " ".join((candidate_output or "").strip().split())


def normalize_final_reply_review_scope(scope: Any = None) -> str:
    """Normalize request/context/profile scope so cache hits are not text-only."""
    if scope is None:
        return ""
    try:
        return json.dumps(scope, sort_keys=True, default=str, separators=(",", ":"))
    except TypeError:
        return str(scope)


def cached_final_reply_review(agent_context, candidate_output: str, review_scope: Any = None) -> dict | None:
    """Return a cached checker verdict for the same candidate reply when present."""
    cached = getattr(agent_context, "final_reply_review", None)
    normalized = normalize_final_reply_candidate(candidate_output)
    normalized_scope = normalize_final_reply_review_scope(review_scope)
    if (
        isinstance(cached, dict)
        and cached.get("candidate_output") == normalized
        and cached.get("review_scope", "") == normalized_scope
    ):
        review = cached.get("review")
        if isinstance(review, dict):
            return review
    return None


def cache_final_reply_review(agent_context, candidate_output: str, review: dict, review_scope: Any = None) -> dict:
    """Persist the checker verdict for the current candidate reply on AgentRun context."""
    agent_context.final_reply_review = {
        "candidate_output": normalize_final_reply_candidate(candidate_output),
        "review_scope": normalize_final_reply_review_scope(review_scope),
        "review": review,
    }
    return review


def continuation_gate_nudge(message: str) -> str:
    intent = extract_latest_user_intent(message)
    return (
        "[System: Continuation gate triggered. "
        "Your latest draft is not yet resolved enough to surface as the final coordinator message. "
        "Keep working toward completion for the user's latest request below. "
        "Do not stop with a partial-progress update or a request for permission to continue. "
        "Either finish the task, or clearly state the concrete blocker or missing user input that truly prevents completion.\n\n"
        f"Latest user request:\n{intent}]"
    )
