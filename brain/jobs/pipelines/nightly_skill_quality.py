"""Nightly skill-quality improvement planner shell.

This pipeline intentionally emits an advisory plan only.  It can consume JSON
exports from repositories, dashboards, or tests, then delegates the actual
decision logic to :mod:`brain.systems.skills.improvement`.
"""
from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from brain.systems.learning.budget import LearningBudgetLedger, LearningBudgetPolicy
from brain.systems.skills.improvement import SkillImprovementPolicy, plan_skill_improvements


def run_nightly_skill_quality(
    *,
    target_date: date | None = None,
    skills: Sequence[Any] = (),
    quality_scores: Sequence[Any] | Mapping[str, Any] = (),
    missing_context_signals: Sequence[Any] = (),
    agent_draft_skills: Sequence[Any] | None = None,
    bundle_update_candidates: Sequence[Any] = (),
    repeated_patterns: Sequence[Any] = (),
    eval_cases_by_skill: Mapping[str, Sequence[Any]] | None = None,
    policy: SkillImprovementPolicy | None = None,
    budget_policy: LearningBudgetPolicy | None = None,
    ledger: LearningBudgetLedger | None = None,
    use_night_budget: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a JSON-safe skill improvement plan for nightly work."""

    clock = now or datetime.now(timezone.utc)
    plan = plan_skill_improvements(
        skills=skills,
        quality_scores=quality_scores,
        missing_context_signals=missing_context_signals,
        agent_draft_skills=agent_draft_skills,
        bundle_update_candidates=bundle_update_candidates,
        repeated_patterns=repeated_patterns,
        eval_cases_by_skill=eval_cases_by_skill,
        policy=policy,
        budget_policy=budget_policy,
        ledger=ledger,
        use_night_budget=use_night_budget,
    )
    payload = plan.to_payload()
    payload.update(
        {
            "pipeline": "nightly_skill_quality",
            "target_date": (target_date or clock.date()).isoformat(),
            "mode": "plan_only",
            "llm_calls": 0,
            "mutates_skills": False,
            "mutates_bundle_installations": False,
        }
    )
    return payload


def run_nightly_skill_quality_from_payload(
    payload: Mapping[str, Any] | None,
    *,
    target_date: date | None = None,
    use_night_budget: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the nightly plan from a JSON-ish input payload."""

    payload = payload if isinstance(payload, Mapping) else {}
    return run_nightly_skill_quality(
        target_date=target_date,
        skills=_list(payload.get("skills")),
        quality_scores=_quality_scores(payload.get("quality_scores")),
        missing_context_signals=_list(payload.get("missing_context_signals")),
        agent_draft_skills=_list_or_none(payload.get("agent_draft_skills")),
        bundle_update_candidates=_list(payload.get("bundle_update_candidates")),
        repeated_patterns=_list(payload.get("repeated_patterns")),
        eval_cases_by_skill=_eval_cases_by_skill(payload.get("eval_cases_by_skill")),
        use_night_budget=use_night_budget,
        now=now,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan nightly skill quality improvement work.")
    parser.add_argument("--date", dest="target_date", help="Target date, YYYY-MM-DD. Defaults to today.")
    parser.add_argument("--input-json", help="JSON payload with skills, scores, context misses, and patterns.")
    parser.add_argument("--output-json", help="Write the advisory plan to this file.")
    parser.add_argument("--no-budget", action="store_true", help="Skip L16 night-budget planning.")
    args = parser.parse_args(argv)

    target_date = date.fromisoformat(args.target_date) if args.target_date else None
    payload = _load_json(Path(args.input_json)) if args.input_json else {}
    result = run_nightly_skill_quality_from_payload(
        payload,
        target_date=target_date,
        use_night_budget=not args.no_budget,
    )
    if args.output_json:
        path = Path(args.output_json)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    else:
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _load_json(path: Path) -> Mapping[str, Any]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text())
    return payload if isinstance(payload, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _list_or_none(value: Any) -> list[Any] | None:
    return value if isinstance(value, list) else None


def _quality_scores(value: Any) -> Sequence[Any] | Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    return value if isinstance(value, list) else []


def _eval_cases_by_skill(value: Any) -> dict[str, Sequence[Any]]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): list(items) if isinstance(items, list) else []
        for key, items in value.items()
    }


if __name__ == "__main__":
    raise SystemExit(main())
