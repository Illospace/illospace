#!/usr/bin/env python3
"""Delegation quality tracking — closed-loop delegation metrics.

Tracks delegation outcomes: quality scores, rounds needed, success rates.
Provides logging and querying functions for delegation quality data.

Closes #72 (Closed-Loop Delegation).
"""

import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))

from sqlalchemy import text

from brain.platform.db.repositories.unit_of_work import UnitOfWork


def ensure_table():
    """Create the delegation_quality table if it doesn't exist."""
    with UnitOfWork() as uow:
        uow.session.execute(text("""
            CREATE TABLE IF NOT EXISTS delegation_quality (
                id SERIAL PRIMARY KEY,
                session_key TEXT NOT NULL,
                original_ask TEXT NOT NULL,
                task_delegated TEXT NOT NULL,
                sub_agent_output TEXT,
                quality_score FLOAT DEFAULT 0.0,
                rounds_needed INT DEFAULT 1,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """))
        uow.session.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_delegation_quality_created
            ON delegation_quality (created_at)
        """))
        uow.session.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_delegation_quality_session
            ON delegation_quality (session_key)
        """))


def log_delegation(
    session_key: str,
    original_ask: str,
    task: str,
    output: str,
    score: float,
    rounds: int = 1,
) -> int:
    """Log a delegation outcome.

    Returns the id of the inserted row.
    """
    ensure_table()
    with UnitOfWork() as uow:
        row = uow.session.execute(text("""
            INSERT INTO delegation_quality
                (session_key, original_ask, task_delegated, sub_agent_output, quality_score, rounds_needed)
            VALUES (:session_key, :original_ask, :task, :output, :score, :rounds)
            RETURNING id
        """), {
            "session_key": session_key, "original_ask": original_ask,
            "task": task, "output": output, "score": score, "rounds": rounds,
        }).mappings().first()
        return row["id"]


def get_delegation_stats(days: int = 30) -> dict:
    """Get delegation quality statistics for the last N days.

    Returns:
        dict with avg_score, total_delegations, first_pass_success_rate,
        avg_rounds, and recent list.
    """
    ensure_table()
    with UnitOfWork() as uow:
        row = uow.session.execute(text("""
            SELECT
                COUNT(*) as total,
                COALESCE(AVG(quality_score), 0) as avg_score,
                COALESCE(AVG(rounds_needed), 0) as avg_rounds,
                COUNT(*) FILTER (WHERE rounds_needed = 1 AND quality_score >= 0.5) as first_pass_success
            FROM delegation_quality
            WHERE created_at > NOW() - INTERVAL '1 day' * :days
        """), {"days": days}).mappings().first()

        total = row["total"]
        first_pass_rate = (row["first_pass_success"] / total * 100) if total > 0 else 0.0

        # Recent delegations
        recent = [dict(r) for r in uow.session.execute(text("""
            SELECT session_key, task_delegated, quality_score, rounds_needed, created_at
            FROM delegation_quality
            WHERE created_at > NOW() - INTERVAL '1 day' * :days
            ORDER BY created_at DESC
            LIMIT 10
        """), {"days": days}).mappings().all()]

        return {
            "total_delegations": total,
            "avg_score": round(float(row["avg_score"]), 3),
            "avg_rounds": round(float(row["avg_rounds"]), 2),
            "first_pass_success_rate": round(first_pass_rate, 1),
            "recent": recent,
        }
