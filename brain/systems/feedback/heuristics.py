"""
Heuristic Extraction — Turn execution outcomes into reusable patterns.

After each skill execution, this module analyzes what happened and extracts
"when X, do Y" patterns (heuristics). These are validated over multiple
executions — only consistently useful heuristics survive.

Heuristics are the skill genome's "learned muscle memory." They make skills
more efficient over time by encoding experience as actionable patterns,
not just text memories.

Lifecycle:
  1. Extract candidate heuristic from execution trace
  2. Store with confidence=0.5 (uncertain)
  3. On subsequent executions: if heuristic was relevant and outcome was good,
     confidence += 0.1 (validated). If bad outcome, confidence -= 0.15 (violated).
  4. Heuristics below confidence 0.2 are pruned (deactivated).
  5. High-confidence heuristics (>0.8) get injected into cognitive frames.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import timezone

from sqlalchemy import text

from brain.platform.db.repositories.unit_of_work import UnitOfWork

logger = logging.getLogger("feedback.heuristics")

# Confidence thresholds
INJECT_THRESHOLD = 0.6   # include in cognitive frames
PRUNE_THRESHOLD = 0.2    # deactivate
VALIDATE_BOOST = 0.1     # confidence boost on validation
VIOLATE_PENALTY = 0.15   # confidence penalty on violation

# Graduation thresholds — heuristics above these become mandatory skill steps
GRADUATION_CONFIDENCE = 0.9
GRADUATION_MIN_VALIDATIONS = 8
GRADUATION_MIN_SOURCES = 3
GRADUATION_COOLDOWN_DAYS = 7
DEMOTION_CONFIDENCE = 0.7

_TASK_STOPWORDS = {
    "a", "an", "and", "build", "create", "do", "fix", "for", "in", "into",
    "investigate", "make", "of", "on", "or", "please", "the", "to",
    "update", "with", "write",
}


def _task_tokens(task: str, *, max_terms: int) -> list[str]:
    tokens: list[str] = []
    for raw in re.findall(r"[a-z0-9]+", task.lower()):
        if raw in _TASK_STOPWORDS or len(raw) < 3:
            continue
        if raw not in tokens:
            tokens.append(raw)
        if len(tokens) >= max_terms:
            break
    return tokens


def task_family_from_text(task: str, *, max_terms: int = 4) -> str:
    """Derive a narrow, stable family label from task text.

    Habits should be narrower than skills, so we keep only the most informative
    task terms rather than trying to infer a broad semantic category.
    """
    if not task:
        return "general"

    tokens = _task_tokens(task, max_terms=max_terms)
    if not tokens:
        return "general"
    return "-".join(tokens)


def task_markers_from_text(task: str, *, max_terms: int = 6) -> list[str]:
    """Return compact task markers suitable for runtime signatures."""
    return _task_tokens(task, max_terms=max_terms)


def _as_utc(dt):
    """Normalize DB datetimes so naive and aware timestamps compare safely."""
    if dt is None:
        return None
    tzinfo = getattr(dt, "tzinfo", None)
    return dt.replace(tzinfo=timezone.utc) if tzinfo is None else dt.astimezone(timezone.utc)


def extract_heuristics(
    task: str,
    skill_name: str,
    outcome: str,
    success: bool,
    execution_trace: str | None = None,
) -> list[dict]:
    """Extract candidate heuristics from a completed execution.

    Uses Ollama (free, local) to analyze what happened and propose
    reusable "when X, do Y" patterns.

    Returns list of {"condition": str, "action": str} dicts.
    """
    if not success:
        # Failures produce anti-heuristics: "when X, DON'T do Y"
        return _extract_failure_heuristics(task, skill_name, outcome, execution_trace)

    prompt = (
        f"Analyze this successful task execution and extract 1-3 reusable patterns.\n\n"
        f"SKILL: {skill_name}\n"
        f"TASK: {task[:500]}\n"
        f"OUTCOME: {outcome[:500]}\n"
    )
    if execution_trace:
        prompt += f"TRACE: {execution_trace[:1000]}\n"

    prompt += (
        f"\nExtract patterns as JSON array. Each pattern has:\n"
        f'- "condition": When does this pattern apply? (be specific)\n'
        f'- "action": What should be done? (be actionable)\n\n'
        f"Only extract patterns that would be USEFUL for similar future tasks. "
        f"Skip obvious/generic advice. Return [] if nothing worth extracting.\n"
        f'Example: [{{"condition": "when modifying database schemas with existing views", '
        f'"action": "check for dependent views before ALTER TABLE"}}]\n\n'
        f"JSON:"
    )

    candidates = _call_gpu_server(prompt)
    if not candidates:
        return []

    # Filter: must have both condition and action, reasonable length
    return [
        h for h in candidates
        if isinstance(h, dict)
        and h.get("condition") and h.get("action")
        and len(h["condition"]) > 10 and len(h["action"]) > 10
        and len(h["condition"]) < 200 and len(h["action"]) < 200
    ]


def _extract_failure_heuristics(
    task: str,
    skill_name: str,
    error: str,
    trace: str | None = None,
) -> list[dict]:
    """Extract anti-heuristics from failures: "when X, avoid Y"."""
    prompt = (
        f"A task FAILED. Extract 1-2 patterns to AVOID in similar situations.\n\n"
        f"SKILL: {skill_name}\n"
        f"TASK: {task[:500]}\n"
        f"ERROR: {error[:500]}\n"
    )
    if trace:
        prompt += f"TRACE: {trace[:500]}\n"

    prompt += (
        f"\nExtract anti-patterns as JSON array:\n"
        f'- "condition": When does this failure pattern occur?\n'
        f'- "action": What should be done INSTEAD to avoid it?\n\n'
        f"JSON:"
    )

    candidates = _call_gpu_server(prompt)
    if not candidates:
        return []

    return [
        h for h in candidates
        if isinstance(h, dict)
        and h.get("condition") and h.get("action")
        and len(h["condition"]) > 10 and len(h["action"]) > 10
    ]


async def store_heuristics(skill_name: str, candidates: list[dict]):
    """Store candidate heuristics, deduplicating against existing ones."""
    if not candidates:
        return

    try:
        async with UnitOfWork() as uow:
            # Load existing heuristics for dedup
            existing = (await uow.session.execute(text(
                "SELECT id, condition, action FROM skill_heuristics "
                "WHERE skill_name = :skill AND active"
            ), {"skill": skill_name})).mappings().all()
            existing_texts = {
                f"{r['condition'].lower().strip()}|{r['action'].lower().strip()}"
                for r in existing
            }

            stored = 0
            for h in candidates:
                key = f"{h['condition'].lower().strip()}|{h['action'].lower().strip()}"
                if key in existing_texts:
                    # Duplicate — boost source_count instead
                    for ex in existing:
                        if f"{ex['condition'].lower().strip()}|{ex['action'].lower().strip()}" == key:
                            await uow.session.execute(text(
                                "UPDATE skill_heuristics SET source_count = source_count + 1, "
                                "updated_at = NOW() WHERE id = :id"
                            ), {"id": ex["id"]})
                            break
                    continue

                await uow.session.execute(text("""
                    INSERT INTO skill_heuristics (skill_name, condition, action)
                    VALUES (:skill, :condition, :action)
                """), {"skill": skill_name, "condition": h["condition"], "action": h["action"]})
                stored += 1

            # Update skill heuristic count
            if stored > 0:
                await uow.session.execute(text(
                    "UPDATE skills SET heuristic_count = ("
                    "  SELECT COUNT(*) FROM skill_heuristics "
                    "  WHERE skill_name = :skill AND active"
                    ") WHERE name = :skill"
                ), {"skill": skill_name})

            if stored:
                logger.info(f"Stored {stored} new heuristics for skill '{skill_name}'")

    except Exception as e:
        logger.warning(f"Failed to store heuristics: {e}")


async def validate_heuristics(skill_name: str, success: bool):
    """Update confidence of active heuristics based on execution outcome.

    If the skill succeeded, all active heuristics for it get a small boost.
    If it failed, they get a penalty. Over time, useful heuristics rise
    and useless ones get pruned.
    """
    try:
        async with UnitOfWork() as uow:
            if success:
                await uow.session.execute(text("""
                    UPDATE skill_heuristics
                    SET confidence = LEAST(1.0, confidence + :boost),
                        validated_count = validated_count + 1,
                        last_validated = NOW(),
                        updated_at = NOW()
                    WHERE skill_name = :skill AND active
                """), {"boost": VALIDATE_BOOST, "skill": skill_name})
            else:
                await uow.session.execute(text("""
                    UPDATE skill_heuristics
                    SET confidence = GREATEST(0.0, confidence - :penalty),
                        violated_count = violated_count + 1,
                        last_violated = NOW(),
                        updated_at = NOW()
                    WHERE skill_name = :skill AND active
                """), {"penalty": VIOLATE_PENALTY, "skill": skill_name})

            # Prune low-confidence heuristics
            pruned = (await uow.session.execute(text("""
                UPDATE skill_heuristics
                SET active = FALSE, updated_at = NOW()
                WHERE skill_name = :skill AND active AND confidence < :threshold
                RETURNING id
            """), {"skill": skill_name, "threshold": PRUNE_THRESHOLD})).all()
            if pruned:
                logger.info(f"Pruned {len(pruned)} low-confidence heuristics for '{skill_name}'")

    except Exception as e:
        logger.warning(f"Failed to validate heuristics: {e}")


async def get_active_heuristics(skill_name: str, min_confidence: float = INJECT_THRESHOLD) -> list[dict]:
    """Get high-confidence heuristics for injection into cognitive frames."""
    try:
        async with UnitOfWork() as uow:
            rows = (await uow.session.execute(text("""
                SELECT condition, action, confidence, validated_count, source_count
                FROM skill_heuristics
                WHERE skill_name = :skill AND active AND confidence >= :min_conf
                  AND (graduated = FALSE OR graduated IS NULL)
                ORDER BY confidence DESC
                LIMIT 10
            """), {"skill": skill_name, "min_conf": min_confidence})).mappings().all()
            return [dict(r) for r in rows]
    except Exception:
        return []


async def update_skill_fitness(skill_name: str):
    """Recompute and store fitness score for a skill.

    Fitness = weighted combination of success rate, efficiency, heuristic quality.
    """
    try:
        async with UnitOfWork() as uow:
            skill = (await uow.session.execute(text("""
                SELECT use_count, success_count, failure_count, confidence,
                       heuristic_count
                FROM skills WHERE name = :name AND NOT archived
            """), {"name": skill_name})).mappings().first()
            if not skill:
                return

            use = skill["use_count"] or 0
            succ = skill["success_count"] or 0
            success_rate = succ / max(use, 1)

            # Get average heuristic confidence
            h_row = (await uow.session.execute(text("""
                SELECT AVG(confidence) as avg_conf, COUNT(*) as total
                FROM skill_heuristics
                WHERE skill_name = :skill AND active
            """), {"skill": skill_name})).mappings().first()
            heuristic_quality = float(h_row["avg_conf"] or 0.5)
            heuristic_coverage = min(1.0, (h_row["total"] or 0) / 10)  # 10 heuristics = full coverage

            # Get prediction accuracy for this skill
            pred_row = (await uow.session.execute(text("""
                SELECT AVG(1.0 - CAST(payload->>'prediction_error' AS FLOAT)) as accuracy
                FROM agent_run_artifacts
                WHERE artifact_type = 'prediction'
                  AND payload->>'skill_name' = :skill
                  AND payload->>'resolved_at' IS NOT NULL
                  AND created_at > NOW() - INTERVAL '30 days'
            """), {"skill": skill_name})).mappings().first()
            prediction_accuracy = float(pred_row["accuracy"] or 0.5) if pred_row else 0.5

            # Composite fitness
            fitness = (
                success_rate * 0.35 +
                (skill["confidence"] or 0.5) * 0.15 +
                heuristic_quality * 0.15 +
                heuristic_coverage * 0.15 +
                prediction_accuracy * 0.20
            )

            await uow.session.execute(text(
                "UPDATE skills SET fitness_score = :fitness, updated_at = NOW() WHERE name = :name"
            ), {"fitness": round(fitness, 4), "name": skill_name})
            logger.debug(f"Skill '{skill_name}' fitness updated: {fitness:.3f}")

    except Exception as e:
        logger.warning(f"Failed to update skill fitness: {e}")


# ── Graduation / Demotion ─────────────────────────────────────


async def graduate_heuristics(skill_name: str) -> list[dict]:
    """Promote high-confidence heuristics to mandatory graduated steps.

    Graduated heuristics are stored in skills.graduated_steps (JSONB).
    They're rendered as mandatory rules in the skill procedure at prompt time.
    The heuristic row is marked graduated=TRUE to avoid duplicate injection.

    Returns list of graduated heuristics (for logging).
    """
    from datetime import datetime, timedelta

    graduated = []
    try:
        async with UnitOfWork() as uow:
            candidates = (await uow.session.execute(text("""
                SELECT id, condition, action, confidence, validated_count, source_count, demoted_at
                FROM skill_heuristics
                WHERE skill_name = :skill
                  AND active = TRUE
                  AND graduated = FALSE
                  AND confidence >= :grad_conf
                  AND validated_count >= :min_val
                  AND source_count >= :min_src
            """), {
                "skill": skill_name,
                "grad_conf": GRADUATION_CONFIDENCE,
                "min_val": GRADUATION_MIN_VALIDATIONS,
                "min_src": GRADUATION_MIN_SOURCES,
            })).mappings().all()

            cooldown_cutoff = datetime.now(timezone.utc) - timedelta(days=GRADUATION_COOLDOWN_DAYS)

            for h in candidates:
                # Skip if recently demoted (cooldown)
                demoted_at = _as_utc(h["demoted_at"])
                if demoted_at and demoted_at > cooldown_cutoff:
                    continue

                step = {
                    "heuristic_id": h["id"],
                    "condition": h["condition"],
                    "action": h["action"],
                    "graduated_at": datetime.now(timezone.utc).isoformat(),
                }

                # Add to skills.graduated_steps
                await uow.session.execute(text("""
                    UPDATE skills
                    SET graduated_steps = COALESCE(graduated_steps, '[]'::jsonb) || CAST(:step AS jsonb),
                        updated_at = NOW()
                    WHERE name = :skill
                """), {"step": json.dumps([step]), "skill": skill_name})

                # Mark heuristic as graduated
                await uow.session.execute(text("""
                    UPDATE skill_heuristics
                    SET graduated = TRUE, graduated_at = NOW(), updated_at = NOW()
                    WHERE id = :id
                """), {"id": h["id"]})

                graduated.append(dict(h))
                logger.info(f"Graduated heuristic {h['id']} for skill '{skill_name}': "
                           f"{h['condition']} → {h['action']}")

    except Exception as e:
        logger.warning(f"Heuristic graduation failed for '{skill_name}': {e}")

    return graduated


async def demote_heuristics(skill_name: str) -> list[dict]:
    """Demote graduated heuristics whose confidence has dropped below threshold.

    Removes from skills.graduated_steps and sets graduated=FALSE.
    """
    demoted = []
    try:
        async with UnitOfWork() as uow:
            candidates = (await uow.session.execute(text("""
                SELECT id, condition, action, confidence
                FROM skill_heuristics
                WHERE skill_name = :skill
                  AND graduated = TRUE
                  AND confidence < :threshold
            """), {"skill": skill_name, "threshold": DEMOTION_CONFIDENCE})).mappings().all()

            for h in candidates:
                # Remove from graduated_steps
                await uow.session.execute(text("""
                    UPDATE skills
                    SET graduated_steps = (
                        SELECT COALESCE(jsonb_agg(step), '[]'::jsonb)
                        FROM jsonb_array_elements(COALESCE(graduated_steps, '[]'::jsonb)) AS step
                        WHERE (step->>'heuristic_id')::int != :hid
                    ),
                    updated_at = NOW()
                    WHERE name = :skill
                """), {"hid": h["id"], "skill": skill_name})

                # Ungraduate the heuristic
                await uow.session.execute(text("""
                    UPDATE skill_heuristics
                    SET graduated = FALSE, demoted_at = NOW(), updated_at = NOW()
                    WHERE id = :id
                """), {"id": h["id"]})

                demoted.append(dict(h))
                logger.info(f"Demoted heuristic {h['id']} for skill '{skill_name}' "
                           f"(confidence dropped to {h['confidence']:.2f})")

    except Exception as e:
        logger.warning(f"Heuristic demotion failed for '{skill_name}': {e}")

    return demoted


# ── Nightly Consolidation ────────────────────────────────────

async def nightly_heuristic_review():
    """Nightly review: prune stale heuristics, recompute all skill fitness.

    Called by nightly pipeline to keep the heuristic system healthy.
    - Prunes heuristics that haven't been validated in 30+ days with low confidence
    - Recomputes fitness for all active skills
    - Returns summary stats.
    """
    stats = {"pruned": 0, "skills_updated": 0}

    try:
        # Prune stale low-confidence heuristics (not validated in 30 days)
        async with UnitOfWork() as uow:
            pruned = (await uow.session.execute(text("""
                UPDATE skill_heuristics
                SET active = FALSE, updated_at = NOW()
                WHERE active
                  AND confidence < 0.4
                  AND (last_validated IS NULL OR last_validated < NOW() - INTERVAL '30 days')
                RETURNING id, skill_name
            """))).all()
            stats["pruned"] = len(pruned)
            if pruned:
                logger.info(f"Nightly: pruned {len(pruned)} stale heuristics")

        # Recompute fitness for all active skills
        async with UnitOfWork() as uow:
            rows = (await uow.session.execute(
                text("SELECT name FROM skills WHERE NOT archived")
            )).mappings().all()
            skills = [r["name"] for r in rows]

        for name in skills:
            try:
                await update_skill_fitness(name)
                stats["skills_updated"] += 1
            except Exception as e:
                logger.debug(f"Fitness update failed for '{name}': {e}")

        # Check for graduation/demotion opportunities across all active skills
        for name in skills:
            await graduate_heuristics(name)
            await demote_heuristics(name)

        logger.info(
            f"Nightly heuristic review: pruned={stats['pruned']}, "
            f"skills_updated={stats['skills_updated']}/{len(skills)}"
        )
    except Exception as e:
        logger.warning(f"Nightly heuristic review failed: {e}")

    return stats


async def summarize_skill_heuristics(skill_name: str) -> dict:
    """Summarize persisted heuristic state for a skill.

    This is read-only and feeds higher-level learning surfaces without
    changing the existing heuristic lifecycle.
    """
    try:
        async with UnitOfWork() as uow:
            row = (await uow.session.execute(text("""
                SELECT
                    COUNT(*) FILTER (WHERE active) AS active_count,
                    COUNT(*) FILTER (WHERE graduated) AS graduated_count,
                    AVG(confidence) AS avg_confidence,
                    MAX(confidence) AS max_confidence,
                    MIN(confidence) AS min_confidence,
                    COALESCE(SUM(source_count), 0) AS source_count_total,
                    COALESCE(SUM(validated_count), 0) AS validated_count_total,
                    COALESCE(SUM(violated_count), 0) AS violated_count_total
                FROM skill_heuristics
                WHERE skill_name = :skill
            """), {"skill": skill_name})).mappings().first()
            return dict(row) if row else {
                "active_count": 0,
                "graduated_count": 0,
                "avg_confidence": None,
                "max_confidence": None,
                "min_confidence": None,
                "source_count_total": 0,
                "validated_count_total": 0,
                "violated_count_total": 0,
            }
    except Exception as exc:
        logger.debug("Skill heuristic summary failed for %s: %s", skill_name, exc)
        return {
            "active_count": 0,
            "graduated_count": 0,
            "avg_confidence": None,
            "max_confidence": None,
            "min_confidence": None,
            "source_count_total": 0,
            "validated_count_total": 0,
            "violated_count_total": 0,
        }


# ── GPU Server Helper ─────────────────────────────────────────

def _call_gpu_server(prompt: str) -> list[dict] | None:
    """Call GPU server for heuristic extraction. Returns parsed JSON list."""
    try:
        from brain.platform.gpu_client import get_client
        result = get_client().generate(
            prompt=prompt, max_tokens=400,
            temperature=0.3, think=False, fallback_policy="auto",
        )
    except Exception as e:
        logger.warning(f"GPU server LLM unavailable for heuristics: {e}")
        return None
    if not result:
        return None
    try:
        return json.loads(result)
    except json.JSONDecodeError:
        start = result.find("[")
        end = result.rfind("]")
        if start >= 0 and end > start:
            try:
                return json.loads(result[start:end + 1])
            except json.JSONDecodeError:
                pass
    return None
