"""Brain prompt generation — pattern detection for nightly self-reflection.

Moved from dashboard/queries.py to break the dashboard dependency.
"""
from __future__ import annotations

import json

from sqlalchemy import text

from brain.platform.db.repositories.unit_of_work import UnitOfWork


def generate_brain_prompts() -> list[dict]:
    """Pattern detection: generate prompts from recent data, write to brain_prompts table.
    Called by nightly consolidation. Deduplication via unique partial index.
    """
    prompts_created = []
    with UnitOfWork() as uow:
        # 1. Repeated skill failures (>2 in last 7 days)
        result = uow.session.execute(text(
            """SELECT s.id AS skill_id, s.name,
                      COUNT(*) AS failure_count
               FROM skill_executions se
               JOIN skills s ON s.id = se.skill_id
               WHERE se.outcome = 'failure'
                 AND se.started_at >= NOW() - INTERVAL '7 days'
                 AND NOT s.archived
               GROUP BY s.id, s.name
               HAVING COUNT(*) > 2"""
        ))
        for row in result.mappings().all():
            content = (
                f"I've failed at '{row['name']}' {row['failure_count']} times this week "
                f"— is there a pattern I'm missing or a guardrail I should add?"
            )
            ctx = json.dumps({"skill_id": row["skill_id"], "memory_id": None,
                              "link": f"/skills/{row['skill_id']}"})
            try:
                insert_result = uow.session.execute(text(
                    """INSERT INTO brain_prompts (type, content, context_json)
                       VALUES ('repeated_failure', :content, CAST(:ctx AS jsonb))
                       ON CONFLICT DO NOTHING"""
                ), {"content": content, "ctx": ctx})
                if insert_result.rowcount:
                    prompts_created.append({"type": "repeated_failure", "skill": row["name"]})
            except Exception:
                pass

        # 2. High-churn, low-signal memories (retrieved >5x, salience < 5)
        result = uow.session.execute(text(
            """SELECT id, content
               FROM memories
               WHERE access_count > 5 AND salience < 5 AND NOT archived
               LIMIT 5"""
        ))
        for row in result.mappings().all():
            content = (
                f"Memory '{(row['content'] or '')[:80]}...' has been retrieved {row['access_count']}x "
                f"but has low salience — should its importance be boosted?"
            )
            ctx = json.dumps({"skill_id": None, "memory_id": row["id"], "link": "/memory"})
            try:
                insert_result = uow.session.execute(text(
                    """INSERT INTO brain_prompts (type, content, context_json)
                       VALUES ('low_retrieval', :content, CAST(:ctx AS jsonb))
                       ON CONFLICT DO NOTHING"""
                ), {"content": content, "ctx": ctx})
                if insert_result.rowcount:
                    prompts_created.append({"type": "low_retrieval", "memory_id": row["id"]})
            except Exception:
                pass

        # 3. Skill confidence decline
        result = uow.session.execute(text(
            """SELECT s.id AS skill_id, s.name, s.confidence
               FROM skills s
               WHERE NOT s.archived
                 AND s.confidence IS NOT NULL
                 AND s.confidence < 0.55"""
        ))
        for row in result.mappings().all():
            if (row["confidence"] or 0) < 0.4:
                content = (
                    f"My confidence in '{row['name']}' has dropped to "
                    f"{round((row['confidence'] or 0)*100)}% — do you have guidance on "
                    f"what I should do differently?"
                )
                ctx = json.dumps({"skill_id": row["skill_id"], "memory_id": None,
                                  "link": f"/skills/{row['skill_id']}"})
                try:
                    insert_result = uow.session.execute(text(
                        """INSERT INTO brain_prompts (type, content, context_json)
                           VALUES ('skill_decline', :content, CAST(:ctx AS jsonb))
                           ON CONFLICT DO NOTHING"""
                    ), {"content": content, "ctx": ctx})
                    if insert_result.rowcount:
                        prompts_created.append({"type": "skill_decline", "skill": row["name"]})
                except Exception:
                    pass

    return prompts_created
