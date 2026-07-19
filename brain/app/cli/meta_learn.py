#!/usr/bin/env python3
"""
Illo Meta-Learning System — Skills that teach and evolve each other.

Usage:
    meta_learn.py author --name "skill" --description "..." --procedure "..." --criteria "..."
    meta_learn.py assess --skill "develop"
    meta_learn.py cross-pollinate
    meta_learn.py evolve
"""

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))

from sqlalchemy import text

from brain.kernel import config
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.memory.embeddings import embed_document, embed_query, vec_to_pg

_vec_to_pg = vec_to_pg


# ---------------------------------------------------------------------------
# Meta-learning data store (JSON file for meta-criteria tracking)
# ---------------------------------------------------------------------------

META_STATE_PATH = str(Path(config.PRIVATE_HOME) / "meta-learning" / "meta_state.json")


def _load_meta_state():
    if os.path.exists(META_STATE_PATH):
        with open(META_STATE_PATH) as f:
            return json.load(f)
    return {
        "validation_criteria": {
            "min_procedure_steps": 3,
            "min_criteria_words": 5,
            "max_overlap_similarity": 0.92,
        },
        "author_decisions": [],   # {name, approved, timestamp, skill_id}
        "assess_recommendations": [],  # {skill, suggestions, timestamp}
        "evolve_history": [],
    }


def _save_meta_state(state):
    os.makedirs(os.path.dirname(META_STATE_PATH), exist_ok=True)
    with open(META_STATE_PATH, "w") as f:
        json.dump(state, f, indent=2, default=str)


# ---------------------------------------------------------------------------
# 1. author_skill — Quality gate for new skill creation
# ---------------------------------------------------------------------------

async def author_skill(name, description, procedure, criteria):
    """Validate and gate new skill creation.

    Returns dict with 'approved' (bool) and 'feedback' (list of strings).
    If approved, creates the skill via the DB.
    """
    state = _load_meta_state()
    vc = state["validation_criteria"]
    feedback = []
    approved = True

    # Check procedure has concrete steps
    steps = [s.strip() for s in procedure.replace("\n", ".").split(".")
             if s.strip() and len(s.strip()) > 5]
    if len(steps) < vc["min_procedure_steps"]:
        feedback.append(
            f"Procedure too vague — only {len(steps)} concrete steps "
            f"(minimum {vc['min_procedure_steps']}). Add specific, verifiable steps.")
        approved = False

    # Detect vague language
    vague_phrases = ["do good work", "try hard", "be careful", "do your best",
                     "make it work", "handle appropriately"]
    proc_lower = procedure.lower()
    for phrase in vague_phrases:
        if phrase in proc_lower:
            feedback.append(f"Vague language detected: '{phrase}'. Replace with specific actions.")
            approved = False

    # Check criteria are measurable
    criteria_words = criteria.split()
    if len(criteria_words) < vc["min_criteria_words"]:
        feedback.append(
            f"Success criteria too brief ({len(criteria_words)} words). "
            f"Need at least {vc['min_criteria_words']} words with measurable outcomes.")
        approved = False

    measurable_indicators = ["rate", "count", "time", "percent", "%", "score",
                             "within", "under", "above", "below", "less than",
                             "more than", "zero", "no errors", "passes", "completes"]
    has_measurable = any(ind in criteria.lower() for ind in measurable_indicators)
    if not has_measurable:
        feedback.append(
            "Criteria lack measurable indicators. Include metrics like "
            "'success rate > 80%', 'completes within 5 min', 'zero errors'.")
        approved = False

    # Check overlap with existing skills
    overlap_name = await _check_skill_overlap(name, description, vc["max_overlap_similarity"])
    if overlap_name:
        feedback.append(
            f"High overlap with existing skill '{overlap_name}'. "
            f"Consider extending that skill instead.")
        approved = False

    # Record decision
    decision = {
        "name": name,
        "approved": approved,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "feedback": feedback,
        "skill_id": None,
    }

    # If approved, create the skill
    if approved:
        feedback.append("All validation checks passed.")
        skill_id = await _create_skill_via_db(name, description, procedure)
        decision["skill_id"] = skill_id
        feedback.append(f"Skill created with id={skill_id}.")

    state["author_decisions"].append(decision)
    # Keep last 100 decisions
    state["author_decisions"] = state["author_decisions"][-100:]
    _save_meta_state(state)

    return {"approved": approved, "feedback": feedback, "skill_id": decision["skill_id"]}


async def _check_skill_overlap(name, description, max_similarity):
    """Check if a skill with similar name/description already exists."""
    try:
        emb = embed_query(f"{name}: {description or ''}")
        async with UnitOfWork() as uow:
            row = (await uow.session.execute(text("""
                SELECT name, 1 - (embedding <=> CAST(:emb AS vector)) AS similarity
                FROM skills WHERE NOT archived
                ORDER BY embedding <=> CAST(:emb AS vector) LIMIT 1
            """), {"emb": _vec_to_pg(emb)})).mappings().first()

            if row and row["similarity"] >= max_similarity:
                return row["name"]
    except Exception:
        pass
    return None


async def _create_skill_via_db(name, description, procedure):
    """Create skill directly in DB (mirrors skills.py cmd_create).

    Enforces the centralized skill-creator gate before insertion.
    """
    from brain.systems.skills.gate import enforce_gate
    enforce_gate(name, description, procedure, automated=False, strict=True)

    emb_text = f"{name}: {description or ''} {procedure[:500]}"
    embedding = embed_document(emb_text)
    async with UnitOfWork() as uow:
        row = (await uow.session.execute(text("""
            INSERT INTO skills (name, description, procedure, level, embedding)
            VALUES (:name, :description, :procedure, 'cognitive', CAST(:embedding AS vector))
            RETURNING id
        """), {
            "name": name, "description": description,
            "procedure": procedure, "embedding": _vec_to_pg(embedding),
        })).mappings().first()
        return row["id"]


# ---------------------------------------------------------------------------
# 2. assess_skill — Deep health check
# ---------------------------------------------------------------------------

async def assess_skill(skill_name):
    """Assess skill health: usage stats, pitfalls, dormancy, suggestions."""
    async with UnitOfWork() as uow:
        skill = (await uow.session.execute(text(
            "SELECT * FROM skills WHERE name = :name AND NOT archived"
        ), {"name": skill_name})).mappings().first()
        if not skill:
            return {"error": f"Skill '{skill_name}' not found"}
        skill = dict(skill)

        # Recent executions (last 30 days)
        exec_rows = (await uow.session.execute(text("""
            SELECT outcome, COUNT(*) as cnt,
                   AVG(duration_sec) as avg_dur
            FROM skill_executions
            WHERE skill_id = :skill_id AND started_at >= NOW() - INTERVAL '30 days'
            GROUP BY outcome
        """), {"skill_id": skill["id"]})).mappings().all()
        exec_stats = {row["outcome"]: {"count": row["cnt"], "avg_duration": row["avg_dur"]}
                      for row in exec_rows}

    total_recent = sum(v["count"] for v in exec_stats.values())
    success_recent = exec_stats.get("success", {}).get("count", 0)
    failure_recent = exec_stats.get("failure", {}).get("count", 0)

    # Compute health indicators
    success_rate = skill["success_count"] / max(1, skill["use_count"])
    recent_rate = success_recent / max(1, total_recent)

    pitfalls = skill.get("pitfalls") or []
    refinements = skill.get("refinements") or []

    # Days since last use
    days_idle = None
    if skill["last_used"]:
        last_used = skill["last_used"]
        if last_used.tzinfo is None:
            last_used = last_used.replace(tzinfo=timezone.utc)
        days_idle = (datetime.now(timezone.utc) - last_used).days

    # Build assessment
    status = "healthy"
    suggestions = []

    if days_idle and days_idle > 30:
        status = "dormant"
        suggestions.append(f"Skill unused for {days_idle} days. Consider archiving or reactivating.")
    elif total_recent == 0 and skill["use_count"] > 0:
        status = "dormant"
        suggestions.append("No recent uses despite prior activity. May need updated triggers.")

    if recent_rate < 0.5 and total_recent >= 3:
        status = "underperforming"
        suggestions.append(
            f"Recent success rate {recent_rate:.0%} is low. "
            f"Review procedure for gaps or outdated steps.")

    if failure_recent > success_recent and total_recent >= 2:
        status = "failing"
        suggestions.append("More failures than successes recently. Procedure needs revision.")

    if len(pitfalls) > 5:
        suggestions.append(
            f"{len(pitfalls)} pitfalls accumulated. Consider consolidating or "
            f"rewriting procedure to address recurring issues.")

    # Trend: compare recent rate vs overall
    if total_recent >= 3 and success_rate > 0:
        trend = recent_rate - success_rate
        if trend < -0.2:
            suggestions.append(
                f"Declining performance: recent {recent_rate:.0%} vs overall {success_rate:.0%}.")
        elif trend > 0.2:
            suggestions.append(
                f"Improving: recent {recent_rate:.0%} vs overall {success_rate:.0%}. Good trend.")

    if not suggestions:
        suggestions.append("Skill performing within expected parameters.")

    result = {
        "skill": skill_name,
        "status": status,
        "maturity": skill["maturity"],
        "confidence": skill["confidence"],
        "use_count": skill["use_count"],
        "success_rate": round(success_rate, 3),
        "recent_uses_30d": total_recent,
        "recent_success_rate": round(recent_rate, 3) if total_recent else None,
        "days_idle": days_idle,
        "pitfall_count": len(pitfalls),
        "refinement_count": len(refinements),
        "suggestions": suggestions,
    }

    # Record for evolve_meta tracking
    state = _load_meta_state()
    state["assess_recommendations"].append({
        "skill": skill_name,
        "status": status,
        "suggestions": suggestions,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    state["assess_recommendations"] = state["assess_recommendations"][-200:]
    _save_meta_state(state)

    return result


# ---------------------------------------------------------------------------
# 3. cross_pollinate — Find transfer opportunities between skills
# ---------------------------------------------------------------------------

async def cross_pollinate():
    """Analyze skills for shared pitfalls and complementary patterns."""
    async with UnitOfWork() as uow:
        rows = (await uow.session.execute(text("""
            SELECT id, name, description, procedure, pitfalls, refinements,
                   use_count, success_count, failure_count, maturity, confidence
            FROM skills WHERE NOT archived
            ORDER BY use_count DESC
        """))).mappings().all()
        skills = [dict(r) for r in rows]

    if len(skills) < 2:
        return {"notes": ["Need at least 2 active skills to cross-pollinate."],
                "shared_pitfalls": [], "transfer_suggestions": []}

    # Find shared pitfalls
    shared_pitfalls = []
    pitfall_map = {}
    for s in skills:
        pits = s.get("pitfalls") or []
        for p in pits:
            pit_text = p if isinstance(p, str) else p.get("text", str(p))
            text_lower = pit_text.lower()[:100]
            if text_lower not in pitfall_map:
                pitfall_map[text_lower] = []
            pitfall_map[text_lower].append(s["name"])

    for pit_text, skill_names in pitfall_map.items():
        if len(skill_names) > 1:
            shared_pitfalls.append({
                "pitfall": pit_text,
                "affected_skills": list(set(skill_names)),
            })

    # Find complementary patterns: high-performing skill + struggling skill
    transfer_suggestions = []
    strong = [s for s in skills if s["use_count"] >= 5
              and s["success_count"] / max(1, s["use_count"]) >= 0.8]
    weak = [s for s in skills if s["use_count"] >= 3
            and s["success_count"] / max(1, s["use_count"]) < 0.6]

    for w in weak:
        w_pits = set()
        for p in (w.get("pitfalls") or []):
            t = p if isinstance(p, str) else p.get("text", str(p))
            w_pits.add(t.lower()[:80])

        for s in strong:
            s_refs = set()
            for r in (s.get("refinements") or []):
                t = r if isinstance(r, str) else r.get("change", str(r))
                s_refs.add(t.lower()[:80])

            # If strong skill has refinements that address weak skill's pitfalls
            overlap = w_pits & s_refs
            if overlap:
                transfer_suggestions.append({
                    "from_skill": s["name"],
                    "to_skill": w["name"],
                    "reason": f"'{s['name']}' has refinements addressing pitfalls in '{w['name']}'",
                    "overlapping_topics": list(overlap)[:3],
                })

        # Also suggest if weak skill could benefit from strong skill's procedure patterns
        if not any(t["to_skill"] == w["name"] for t in transfer_suggestions):
            if strong:
                best = max(strong, key=lambda x: x["success_count"] / max(1, x["use_count"]))
                transfer_suggestions.append({
                    "from_skill": best["name"],
                    "to_skill": w["name"],
                    "reason": f"'{w['name']}' is underperforming. Study '{best['name']}' patterns.",
                })

    notes = []
    if shared_pitfalls:
        notes.append(f"Found {len(shared_pitfalls)} shared pitfall patterns across skills.")
    if transfer_suggestions:
        notes.append(f"Found {len(transfer_suggestions)} cross-training opportunities.")
    if not shared_pitfalls and not transfer_suggestions:
        notes.append("No cross-pollination opportunities detected. Skills are independent.")

    return {
        "notes": notes,
        "shared_pitfalls": shared_pitfalls,
        "transfer_suggestions": transfer_suggestions,
    }


# ---------------------------------------------------------------------------
# 4. evolve_meta — Recursive self-improvement of meta-learning criteria
# ---------------------------------------------------------------------------

async def evolve_meta():
    """Evolve meta-learning criteria based on outcomes of past decisions."""
    state = _load_meta_state()
    vc = state["validation_criteria"]
    changes = []

    # Evaluate author decisions: did approved skills perform well?
    approved = [d for d in state["author_decisions"] if d["approved"] and d.get("skill_id")]
    if len(approved) >= 3:
        try:
            async with UnitOfWork() as uow:
                skill_ids = [d["skill_id"] for d in approved[-20:]]
                rows = (await uow.session.execute(text("""
                    SELECT id, use_count, success_count, failure_count
                    FROM skills WHERE id = ANY(:skill_ids)
                """), {"skill_ids": skill_ids})).mappings().all()
                skill_perf = {r["id"]: dict(r) for r in rows}

            good = 0
            bad = 0
            for d in approved[-20:]:
                sid = d["skill_id"]
                if sid in skill_perf:
                    s = skill_perf[sid]
                    rate = s["success_count"] / max(1, s["use_count"])
                    if s["use_count"] >= 2:
                        if rate >= 0.7:
                            good += 1
                        else:
                            bad += 1

            if bad > good and (good + bad) >= 3:
                # Too many bad approvals -- tighten criteria
                vc["min_procedure_steps"] = min(vc["min_procedure_steps"] + 1, 8)
                vc["min_criteria_words"] = min(vc["min_criteria_words"] + 2, 15)
                changes.append(
                    f"Tightened criteria: min_steps={vc['min_procedure_steps']}, "
                    f"min_criteria_words={vc['min_criteria_words']} "
                    f"(approved skills underperforming: {bad}/{good+bad})")
            elif good > bad * 2 and (good + bad) >= 3:
                # Quality is good -- slightly relax
                vc["min_procedure_steps"] = max(vc["min_procedure_steps"] - 1, 2)
                changes.append(
                    f"Relaxed criteria: min_steps={vc['min_procedure_steps']} "
                    f"(approved skills performing well: {good}/{good+bad})")
        except Exception as e:
            changes.append(f"Could not evaluate author decisions: {e}")

    # Evaluate rejected skills — were rejections warranted?
    rejected = [d for d in state["author_decisions"] if not d["approved"]]
    if rejected:
        rejection_reasons = {}
        for d in rejected[-20:]:
            for f in d.get("feedback", []):
                key = f[:50]
                rejection_reasons[key] = rejection_reasons.get(key, 0) + 1
        top_reason = max(rejection_reasons, key=rejection_reasons.get) if rejection_reasons else None
        if top_reason:
            changes.append(f"Top rejection reason: '{top_reason}' ({rejection_reasons[top_reason]}x)")

    # Evaluate assess recommendations
    recent_assessments = state.get("assess_recommendations", [])[-50:]
    status_counts = {}
    for a in recent_assessments:
        st = a.get("status", "unknown")
        status_counts[st] = status_counts.get(st, 0) + 1
    if status_counts:
        changes.append(f"Assessment distribution: {json.dumps(status_counts)}")

    if not changes:
        changes.append("No meta-evolution needed — insufficient data for adjustments.")

    # Record evolution
    state["validation_criteria"] = vc
    state["evolve_history"].append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "changes": changes,
        "criteria_snapshot": dict(vc),
    })
    state["evolve_history"] = state["evolve_history"][-50:]
    _save_meta_state(state)

    return {"changes": changes, "current_criteria": vc}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(description="Illo Meta-Learning System")
    subparsers = parser.add_subparsers(dest="command")

    # author
    p_author = subparsers.add_parser("author", help="Gate new skill creation")
    p_author.add_argument("--name", "-n", required=True)
    p_author.add_argument("--description", "-d", default="")
    p_author.add_argument("--procedure", "-p", required=True)
    p_author.add_argument("--criteria", "-c", required=True)

    # assess
    p_assess = subparsers.add_parser("assess", help="Deep skill health check")
    p_assess.add_argument("--skill", "-s", required=True)

    # cross-pollinate
    subparsers.add_parser("cross-pollinate", help="Find cross-training opportunities")

    # evolve
    subparsers.add_parser("evolve", help="Evolve meta-learning criteria")

    args = parser.parse_args()

    if args.command == "author":
        result = await author_skill(args.name, args.description, args.procedure, args.criteria)
    elif args.command == "assess":
        result = await assess_skill(args.skill)
    elif args.command == "cross-pollinate":
        result = await cross_pollinate()
    elif args.command == "evolve":
        result = await evolve_meta()
    else:
        parser.print_help()
        return

    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main())
