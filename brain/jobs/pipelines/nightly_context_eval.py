"""Nightly context-policy evaluator.

This pipeline replays recent trajectory eval cases against shadow context
policy candidates and emits candidate decision payloads.  It never mutates
runtime context-policy flags.
"""
from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from typing import Any

from brain.systems.learning.context_evals import (
    ContextPolicyCandidate,
    ContextPolicyEvalThresholds,
    evaluate_context_policy_candidates,
)


def gather_recent_context_policy_eval_sources(
    *,
    limit: int = 50,
    org_id: str | None = None,
    user_id: str | None = None,
    status: str = "active",
) -> list[Mapping[str, Any]]:
    """Return no persisted sources: learning eval-case storage was removed."""
    return []


def run_nightly_context_policy_eval(
    *,
    target_date: date | None = None,
    sources: Sequence[Mapping[str, Any] | Any] | None = None,
    candidate: Mapping[str, Any] | ContextPolicyCandidate | None = None,
    thresholds: Mapping[str, Any] | ContextPolicyEvalThresholds | None = None,
    limit: int = 50,
    org_id: str | None = None,
    user_id: str | None = None,
    persist_candidates: bool = False,
    load_recent: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run the nightly evaluator and return a JSON-safe payload."""
    clock = now or datetime.now(timezone.utc)
    loaded_sources: Sequence[Mapping[str, Any] | Any] = list(sources or [])
    source_status = "provided"
    source_error = None
    if not loaded_sources and load_recent:
        try:
            loaded_sources = gather_recent_context_policy_eval_sources(
                limit=limit,
                org_id=org_id,
                user_id=user_id,
            )
            source_status = "loaded_recent_eval_cases"
        except Exception as exc:
            loaded_sources = []
            source_status = "load_failed"
            source_error = str(exc)

    evaluation = evaluate_context_policy_candidates(
        loaded_sources,
        candidates=[ContextPolicyCandidate.from_value(candidate)],
        thresholds=thresholds,
        evaluated_at=clock,
    )
    persisted: list[dict[str, Any]] = []
    persist_error = None
    if persist_candidates:
        persist_error = "candidate persistence is unavailable"

    payload = {
        "pipeline": "nightly_context_eval",
        "mode": "shadow_candidate_decision",
        "target_date": (target_date or clock.date()).isoformat(),
        "evaluated_at": clock.isoformat(),
        "source_status": source_status,
        "source_error": source_error,
        "persist_candidates": persist_candidates,
        "persisted_candidates": persisted,
        "persist_error": persist_error,
        "active_policy_changed": False,
        "runtime_flags_mutated": False,
        "evaluation": evaluation,
    }
    return payload


def _parse_json_mapping(value: str | None, *, label: str) -> dict[str, Any] | None:
    if not value:
        return None
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError(f"{label} must decode to a JSON object")
    return parsed


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run nightly context-policy shadow evaluation.")
    parser.add_argument("--date", dest="target_date", help="Target date for the nightly run (YYYY-MM-DD).")
    parser.add_argument("--limit", type=int, default=50, help="Maximum recent eval cases to replay.")
    parser.add_argument("--org-id", dest="org_id")
    parser.add_argument("--user-id", dest="user_id")
    parser.add_argument("--candidate-json", help="JSON object overriding the default policy candidate.")
    parser.add_argument("--thresholds-json", help="JSON object overriding default promotion thresholds.")
    parser.add_argument("--persist-candidates", action="store_true", help="Persist reviewable policy candidates.")
    parser.add_argument("--no-db", action="store_true", help="Do not load recent eval cases from the database.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    payload = run_nightly_context_policy_eval(
        target_date=_parse_date(args.target_date),
        candidate=_parse_json_mapping(args.candidate_json, label="candidate-json"),
        thresholds=_parse_json_mapping(args.thresholds_json, label="thresholds-json"),
        limit=args.limit,
        org_id=args.org_id,
        user_id=args.user_id,
        persist_candidates=args.persist_candidates,
        load_recent=not args.no_db,
    )
    print(json.dumps(payload, sort_keys=True, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
