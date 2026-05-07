#!/usr/bin/env python3
"""
Co-evolutionary Doer-Critic System.

Every skill execution can have a critic review. Both doer and critic skill
scores evolve based on real outcomes, creating a co-evolutionary feedback loop.

Key insight: critics have their own tracked performance (precision, recall,
noise ratio). When a critic flags a non-issue -> its skill degrades. When it
misses a real issue -> it degrades. This prevents rubber-stamping AND nitpicking.

Usage:
    critic_system.py record-review <execution_id> --findings '...' --scores '{}'
    critic_system.py record-outcome <execution_id> --outcome success --source user_feedback
    critic_system.py context <skill_name>            # past misses/false positives for prompt
    critic_system.py health                          # precision/recall/noise per skill
    critic_system.py migrate                         # create DB tables
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))  # repo root

from sqlalchemy import text

from brain.platform.db.repositories.unit_of_work import UnitOfWork
# ---------------------------------------------------------------------------
# Schema Migration
# ---------------------------------------------------------------------------

MIGRATION_SQL = """
-- Critic reviews: one per doer execution
CREATE TABLE IF NOT EXISTS critic_reviews (
    id              SERIAL PRIMARY KEY,
    execution_id    INTEGER NOT NULL REFERENCES skill_executions(id),
    critic_skill_id INTEGER REFERENCES skills(id),
    findings        JSONB NOT NULL DEFAULT '[]',
    scores          JSONB NOT NULL DEFAULT '{}',
    verdict         VARCHAR(20) NOT NULL DEFAULT 'approve',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Execution outcomes: ground truth for both doer and critic
CREATE TABLE IF NOT EXISTS execution_outcomes (
    id                SERIAL PRIMARY KEY,
    execution_id      INTEGER NOT NULL REFERENCES skill_executions(id),
    critic_review_id  INTEGER REFERENCES critic_reviews(id),
    outcome           VARCHAR(20) NOT NULL CHECK (outcome IN ('success', 'failure', 'partial')),
    outcome_source    VARCHAR(50) NOT NULL DEFAULT 'user_feedback',
    notes             TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_critic_reviews_execution ON critic_reviews(execution_id);
CREATE INDEX IF NOT EXISTS idx_execution_outcomes_execution ON execution_outcomes(execution_id);
CREATE INDEX IF NOT EXISTS idx_execution_outcomes_critic ON execution_outcomes(critic_review_id);
"""


def migrate():
    """Create critic system tables."""
    with UnitOfWork() as uow:
        uow.session.execute(text(MIGRATION_SQL))
    print("Critic system tables created/verified.")


# ---------------------------------------------------------------------------
# Core Operations
# ---------------------------------------------------------------------------

def record_critic_review(execution_id: int, findings: list, scores: dict,
                         critic_skill_id: int | None = None,
                         verdict: str = "approve") -> int:
    """Record a critic's review of a doer execution.

    Args:
        execution_id: The doer's skill_execution id.
        findings: List of dicts, each with 'issue', 'severity', 'category'.
        scores: Dict of quality scores the critic assigned.
        critic_skill_id: The skill id of the critic (for tracking critic performance).
        verdict: 'approve', 'reject', or 'revise'.

    Returns:
        The critic_review id.
    """
    with UnitOfWork() as uow:
        row = uow.session.execute(text("""
            INSERT INTO critic_reviews (execution_id, critic_skill_id, findings, scores, verdict)
            VALUES (:execution_id, :critic_skill_id, :findings, :scores, :verdict)
            RETURNING id
        """), {
            "execution_id": execution_id, "critic_skill_id": critic_skill_id,
            "findings": json.dumps(findings), "scores": json.dumps(scores),
            "verdict": verdict,
        }).mappings().first()
        return row["id"]


def record_outcome(execution_id: int, outcome: str, source: str = "user_feedback",
                   notes: str | None = None, critic_review_id: int | None = None) -> int:
    """Record the ground-truth outcome of an execution.

    This triggers score updates for both doer and critic.

    Returns:
        The execution_outcome id.
    """
    if outcome not in ("success", "failure", "partial"):
        raise ValueError(f"Invalid outcome: {outcome}")

    # Auto-find critic review if not provided
    if critic_review_id is None:
        with UnitOfWork() as uow:
            row = uow.session.execute(text("""
                SELECT id FROM critic_reviews WHERE execution_id = :execution_id
                ORDER BY created_at DESC LIMIT 1
            """), {"execution_id": execution_id}).mappings().first()
            if row:
                critic_review_id = row["id"]

    with UnitOfWork() as uow:
        row = uow.session.execute(text("""
            INSERT INTO execution_outcomes (execution_id, critic_review_id, outcome, outcome_source, notes)
            VALUES (:execution_id, :critic_review_id, :outcome, :outcome_source, :notes)
            RETURNING id
        """), {
            "execution_id": execution_id, "critic_review_id": critic_review_id,
            "outcome": outcome, "outcome_source": source, "notes": notes,
        }).mappings().first()
        outcome_id = row["id"]

    # Update both doer and critic skills
    update_both_skills(execution_id, outcome, critic_review_id)
    return outcome_id


def update_both_skills(execution_id: int, outcome: str,
                       critic_review_id: int | None = None):
    """Adjust skill scores for both doer and critic based on outcome.

    Doer: standard success/failure/partial adjustment (already handled by skills.py).
    Critic: adjusted based on whether findings aligned with reality.
    """
    if critic_review_id is None:
        return

    with UnitOfWork() as uow:
        # Get the critic review
        review = uow.session.execute(text(
            "SELECT * FROM critic_reviews WHERE id = :id"
        ), {"id": critic_review_id}).mappings().first()
        if not review or not review["critic_skill_id"]:
            return

        critic_skill_id = review["critic_skill_id"]
        findings = review["findings"] if isinstance(review["findings"], list) else json.loads(review["findings"])
        verdict = review["verdict"]
        had_findings = len(findings) > 0

        # Determine critic accuracy
        # Case 1: Critic flagged issues + outcome was failure -> critic was RIGHT (true positive)
        # Case 2: Critic flagged issues + outcome was success -> critic was WRONG (false positive)
        # Case 3: Critic approved + outcome was failure -> critic MISSED it (false negative)
        # Case 4: Critic approved + outcome was success -> critic was RIGHT (true negative)

        if verdict in ("reject", "revise") or had_findings:
            if outcome == "failure":
                # True positive -- critic caught a real issue
                _adjust_critic_skill(uow.session, critic_skill_id, "tp")
            else:
                # False positive -- critic flagged a non-issue
                _adjust_critic_skill(uow.session, critic_skill_id, "fp")
        else:
            if outcome == "failure":
                # False negative -- critic missed a real issue
                _adjust_critic_skill(uow.session, critic_skill_id, "fn")
            else:
                # True negative -- critic correctly approved
                _adjust_critic_skill(uow.session, critic_skill_id, "tn")


def _adjust_critic_skill(session, skill_id: int, signal: str):
    """Adjust critic skill counts. Signals: tp, fp, fn, tn.

    We piggyback on the existing skills table:
    - success_count tracks correct predictions (tp + tn)
    - failure_count tracks incorrect predictions (fp + fn)
    - use_count tracks total reviews
    - pitfalls stores critic-specific metadata (false_positives, misses)
    """
    if signal in ("tp", "tn"):
        session.execute(text("""
            UPDATE skills
            SET use_count = use_count + 1,
                success_count = success_count + 1,
                last_used = now(),
                updated_at = now()
            WHERE id = :id
        """), {"id": skill_id})
    elif signal in ("fp", "fn"):
        session.execute(text("""
            UPDATE skills
            SET use_count = use_count + 1,
                failure_count = failure_count + 1,
                last_used = now(),
                updated_at = now()
            WHERE id = :id
        """), {"id": skill_id})


def get_critic_context(skill_name: str, limit: int = 5) -> dict:
    """Get past misses and false positives for a critic skill, for prompt injection.

    Returns:
        {
            "false_positives": [{"task": ..., "finding": ..., "outcome": ...}, ...],
            "misses": [{"task": ..., "outcome_notes": ...}, ...],
            "precision": float,
            "recall": float,
        }
    """
    with UnitOfWork() as uow:
        # Get critic skill id
        row = uow.session.execute(text(
            "SELECT id FROM skills WHERE name = :name"
        ), {"name": skill_name}).mappings().first()
        if not row:
            return {"false_positives": [], "misses": [], "precision": None, "recall": None}
        skill_id = row["id"]

        # False positives: critic flagged issues but outcome was success
        fp_rows = uow.session.execute(text("""
            SELECT se.task_description, cr.findings, eo.outcome, eo.notes
            FROM critic_reviews cr
            JOIN execution_outcomes eo ON eo.critic_review_id = cr.id
            JOIN skill_executions se ON se.id = cr.execution_id
            WHERE cr.critic_skill_id = :skill_id
              AND cr.verdict IN ('reject', 'revise')
              AND eo.outcome = 'success'
            ORDER BY eo.created_at DESC
            LIMIT :limit
        """), {"skill_id": skill_id, "limit": limit}).mappings().all()
        false_positives = [
            {"task": r["task_description"], "findings": r["findings"], "actual_outcome": r["outcome"]}
            for r in fp_rows
        ]

        # Misses: critic approved but outcome was failure
        miss_rows = uow.session.execute(text("""
            SELECT se.task_description, eo.notes, eo.outcome
            FROM critic_reviews cr
            JOIN execution_outcomes eo ON eo.critic_review_id = cr.id
            JOIN skill_executions se ON se.id = cr.execution_id
            WHERE cr.critic_skill_id = :skill_id
              AND cr.verdict = 'approve'
              AND eo.outcome = 'failure'
            ORDER BY eo.created_at DESC
            LIMIT :limit
        """), {"skill_id": skill_id, "limit": limit}).mappings().all()
        misses = [
            {"task": r["task_description"], "outcome_notes": r["notes"]}
            for r in miss_rows
        ]

        # Compute precision and recall
        metrics = _compute_critic_metrics(uow.session, skill_id)

        return {
            "false_positives": false_positives,
            "misses": misses,
            **metrics,
        }


def _compute_critic_metrics(session, skill_id: int) -> dict:
    """Compute precision, recall, noise ratio for a critic skill."""
    # Count signal types from joined data
    row = session.execute(text("""
        SELECT
            COUNT(*) FILTER (WHERE cr.verdict IN ('reject','revise') AND eo.outcome = 'failure') AS tp,
            COUNT(*) FILTER (WHERE cr.verdict IN ('reject','revise') AND eo.outcome IN ('success','partial')) AS fp,
            COUNT(*) FILTER (WHERE cr.verdict = 'approve' AND eo.outcome = 'failure') AS fn,
            COUNT(*) FILTER (WHERE cr.verdict = 'approve' AND eo.outcome IN ('success','partial')) AS tn
        FROM critic_reviews cr
        JOIN execution_outcomes eo ON eo.critic_review_id = cr.id
        WHERE cr.critic_skill_id = :skill_id
    """), {"skill_id": skill_id}).mappings().first()
    tp, fp, fn, tn = row["tp"], row["fp"], row["fn"], row["tn"]

    precision = tp / (tp + fp) if (tp + fp) > 0 else None
    recall = tp / (tp + fn) if (tp + fn) > 0 else None
    total = tp + fp + fn + tn
    noise_ratio = fp / total if total > 0 else None

    return {
        "precision": round(precision, 3) if precision is not None else None,
        "recall": round(recall, 3) if recall is not None else None,
        "noise_ratio": round(noise_ratio, 3) if noise_ratio is not None else None,
        "total_reviews": total,
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
    }


def analyze_critic_health() -> list[dict]:
    """Analyze precision, recall, and noise ratio for ALL critic skills.

    Returns list of dicts with skill name and metrics.
    """
    with UnitOfWork() as uow:
        # Find all skills that have been used as critics
        critic_skills = uow.session.execute(text("""
            SELECT DISTINCT s.id, s.name
            FROM skills s
            JOIN critic_reviews cr ON cr.critic_skill_id = s.id
            ORDER BY s.name
        """)).mappings().all()

        results = []
        for skill in critic_skills:
            metrics = _compute_critic_metrics(uow.session, skill["id"])
            results.append({
                "skill_name": skill["name"],
                **metrics,
            })
        return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Co-evolutionary doer-critic system")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("migrate", help="Create DB tables")

    p_review = sub.add_parser("record-review", help="Record a critic review")
    p_review.add_argument("execution_id", type=int)
    p_review.add_argument("--findings", default="[]")
    p_review.add_argument("--scores", default="{}")
    p_review.add_argument("--critic-skill-id", type=int)
    p_review.add_argument("--verdict", default="approve")

    p_outcome = sub.add_parser("record-outcome", help="Record execution outcome")
    p_outcome.add_argument("execution_id", type=int)
    p_outcome.add_argument("--outcome", required=True)
    p_outcome.add_argument("--source", default="user_feedback")
    p_outcome.add_argument("--notes", default=None)

    p_ctx = sub.add_parser("context", help="Get critic context for prompt injection")
    p_ctx.add_argument("skill_name")

    sub.add_parser("health", help="Analyze critic health across all skills")

    args = parser.parse_args()

    if args.command == "migrate":
        migrate()
    elif args.command == "record-review":
        rid = record_critic_review(
            args.execution_id,
            json.loads(args.findings),
            json.loads(args.scores),
            critic_skill_id=args.critic_skill_id,
            verdict=args.verdict,
        )
        print(f"Critic review #{rid} recorded.")
    elif args.command == "record-outcome":
        oid = record_outcome(args.execution_id, args.outcome, args.source, args.notes)
        print(f"Outcome #{oid} recorded. Doer + critic skills updated.")
    elif args.command == "context":
        ctx = get_critic_context(args.skill_name)
        print(json.dumps(ctx, indent=2, default=str))
    elif args.command == "health":
        health = analyze_critic_health()
        if not health:
            print("No critic data yet.")
        else:
            for h in health:
                print(f"\n{h['skill_name']}:")
                print(f"   Precision: {h['precision']}  Recall: {h['recall']}  Noise: {h['noise_ratio']}")
                print(f"   TP={h['tp']} FP={h['fp']} FN={h['fn']} TN={h['tn']} Total={h['total_reviews']}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
