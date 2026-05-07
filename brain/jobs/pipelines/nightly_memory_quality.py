"""Nightly memory quality pipeline shell.

The current slice gathers existing memory conflict/freshness signals and emits
a deterministic, budget-aware action plan.  It does not call an LLM and does
not mutate memory rows; downstream review/application can replay actions by
their idempotency keys.
"""
from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from datetime import date, datetime, timezone
from typing import Any

from sqlalchemy import or_, select

from brain.platform.db.models.memory import Memory, MemoryContradiction
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.learning.budget import LearningBudgetLedger, LearningBudgetPolicy
from brain.systems.memory.conflict_resolver import resolve_memory_conflicts

_OPEN_CONTRADICTION_STATUSES = ("open", "needs_review")


def gather_memory_quality_inputs(
    *,
    limit: int = 100,
    stale_threshold: float = 0.85,
    now: datetime | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Load conflict rows and stale freshness signals for a nightly run."""
    clock = now or datetime.now(timezone.utc)
    limit = max(1, int(limit or 100))
    with UnitOfWork() as uow:
        contradictions = _fetch_contradictions(uow.session, limit=limit)
        memory_ids = {
            int(row[key])
            for row in contradictions
            for key in ("left_memory_id", "right_memory_id")
            if row.get(key) is not None
        }
        stale_memories = _fetch_stale_memories(
            uow.session,
            limit=limit,
            stale_threshold=stale_threshold,
            now=clock,
        )
        memory_ids.update(int(row["id"]) for row in stale_memories if row.get("id") is not None)
        memories = _fetch_memories(uow.session, sorted(memory_ids)) if memory_ids else []

    freshness_signals = [
        _freshness_signal_from_memory(row, stale_threshold=stale_threshold)
        for row in stale_memories
    ]
    return {
        "contradiction_rows": contradictions,
        "freshness_signals": freshness_signals,
        "memories": memories,
    }


def run_nightly_memory_quality(
    *,
    target_date: date | None = None,
    limit: int = 100,
    stale_threshold: float = 0.85,
    policy: LearningBudgetPolicy | None = None,
    ledger: LearningBudgetLedger | None = None,
    use_night_budget: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a JSON-safe memory conflict action plan for nightly work."""
    clock = now or datetime.now(timezone.utc)
    inputs = gather_memory_quality_inputs(
        limit=limit,
        stale_threshold=stale_threshold,
        now=clock,
    )
    plan = resolve_memory_conflicts(
        contradiction_rows=inputs["contradiction_rows"],
        freshness_signals=inputs["freshness_signals"],
        memories=inputs["memories"],
        policy=policy,
        ledger=ledger,
        use_night_budget=use_night_budget,
    )
    payload = plan.to_dict()
    payload.update(
        {
            "pipeline": "nightly_memory_quality",
            "target_date": (target_date or clock.date()).isoformat(),
            "mode": "plan_only",
            "llm_calls": 0,
            "mutates_memory_rows": False,
        }
    )
    return payload


def _fetch_contradictions(session, *, limit: int) -> list[dict[str, Any]]:
    rows = session.scalars(
        select(MemoryContradiction)
        .where(MemoryContradiction.status.in_(_OPEN_CONTRADICTION_STATUSES))
        .order_by(MemoryContradiction.severity.desc(), MemoryContradiction.created_at.asc())
        .limit(limit)
    ).all()
    return [_model_to_dict(row) for row in rows]


def _fetch_stale_memories(
    session,
    *,
    limit: int,
    stale_threshold: float,
    now: datetime,
) -> list[dict[str, Any]]:
    freshness_floor = max(0.0, min(1.0, 1.0 - stale_threshold))
    rows = session.scalars(
        select(Memory)
        .where(or_(Memory.archived == False, Memory.archived.is_(None)))  # noqa: E712
        .where(Memory.superseded_by.is_(None))
        .where(
            or_(
                Memory.staleness_score >= stale_threshold,
                Memory.freshness_score <= freshness_floor,
                Memory.valid_until < now,
            )
        )
        .order_by(Memory.staleness_score.desc().nullslast(), Memory.last_accessed.asc())
        .limit(limit)
    ).all()
    return [_model_to_dict(row) for row in rows]


def _fetch_memories(session, memory_ids: Sequence[int]) -> list[dict[str, Any]]:
    if not memory_ids:
        return []
    rows = session.scalars(select(Memory).where(Memory.id.in_(memory_ids))).all()
    return [_model_to_dict(row) for row in rows]


def _freshness_signal_from_memory(
    row: dict[str, Any],
    *,
    stale_threshold: float,
) -> dict[str, Any]:
    staleness = _coerce_float(row.get("staleness_score"))
    freshness = _coerce_float(row.get("freshness_score"))
    valid_until = _coerce_datetime(row.get("valid_until"))
    status = "stale"
    reasons: list[str] = []
    confidence = 0.8
    if valid_until is not None and valid_until < datetime.now(timezone.utc):
        reasons.append("valid_until_expired")
        confidence = 1.0
    if staleness is not None and staleness >= stale_threshold:
        reasons.append("staleness_score_high")
        confidence = max(confidence, 0.82)
    if freshness is not None and freshness <= max(0.0, 1.0 - stale_threshold):
        reasons.append("freshness_score_low")
        confidence = max(confidence, 0.78)
    if not reasons:
        status = "possibly_stale"
        reasons.append("source_metadata_possibly_stale")
        confidence = 0.55
    return {
        "id": row.get("id"),
        "memory_id": row.get("id"),
        "status": status,
        "confidence": confidence,
        "score": freshness,
        "staleness_score": staleness,
        "reasons": reasons,
        "source_ref": row.get("source_ref"),
        "subject_ref": row.get("subject_ref"),
    }


def _model_to_dict(model: Any) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for column in model.__table__.columns:
        data[column.name] = getattr(model, column.name)
    return data


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Plan nightly memory conflict resolution work.")
    parser.add_argument("--date", dest="target_date", help="Target date, YYYY-MM-DD. Defaults to today.")
    parser.add_argument("--limit", type=int, default=100, help="Maximum contradictions and stale rows to inspect.")
    parser.add_argument("--stale-threshold", type=float, default=0.85, help="Staleness score treated as stale.")
    parser.add_argument("--no-budget", action="store_true", help="Skip L16 night-budget planning.")
    args = parser.parse_args(argv)

    target_date = date.fromisoformat(args.target_date) if args.target_date else None
    payload = run_nightly_memory_quality(
        target_date=target_date,
        limit=args.limit,
        stale_threshold=args.stale_threshold,
        policy=LearningBudgetPolicy.from_env(),
        use_night_budget=not args.no_budget,
    )
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
