#!/usr/bin/env python3
"""
Illo Meta-Skill System — Self-improving skill graph.

Analyzes the skill system itself to:
1. Detect gaps (recurring tasks without matching skills)
2. Identify weak skills (low success rate, atrophying, low confidence)
3. Suggest improvements based on failure patterns
4. Auto-create skills when patterns emerge (3+ occurrences)
5. Compute meta-metrics for dashboard/wake-up index

Usage:
    python3 meta_skills.py analyze              # full analysis (on-demand)
    python3 meta_skills.py weaknesses           # weakness report only
    python3 meta_skills.py gaps                 # gap detection only
    python3 meta_skills.py metrics              # meta-metrics only
    python3 meta_skills.py auto-create          # create skills from detected gaps
"""

import argparse
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, date
from typing import Optional

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))  # repo root

from sqlalchemy import text

import brain.kernel.config as config
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.systems.memory.embeddings import embed_document, vec_to_pg

# ============================================================
# Constants
# ============================================================

SUCCESS_RATE_THRESHOLD = 0.80
ATROPHY_DAYS = 7
GAP_MIN_OCCURRENCES = 3
MATURITY_SCORES = {"emerging": 1, "developing": 2, "proficient": 3, "expert": 4}


# ============================================================
# 1. Weakness Analysis
# ============================================================

def analyze_weaknesses(skills: list[dict]) -> list[dict]:
    """Identify weak skills: low success rate, atrophying, low confidence."""
    weak = []
    from datetime import timezone
    now = datetime.now(timezone.utc)

    for s in skills:
        issues = []
        use = s["use_count"]
        succ = s["success_count"]
        rate = succ / max(use, 1)

        # Low success rate (only meaningful with 2+ uses)
        if use >= 2 and rate < SUCCESS_RATE_THRESHOLD:
            issues.append("low_success_rate")

        # Atrophying: never used or not used in 7+ days
        if s["last_used"] is None:
            issues.append("atrophying")
        elif s["last_used"] is not None and (now - s["last_used"].replace(tzinfo=s["last_used"].tzinfo or timezone.utc)).days > ATROPHY_DAYS:
            issues.append("atrophying")

        # High pitfalls but low confidence
        pitfalls = s.get("pitfalls") or []
        if len(pitfalls) >= 3 and s["confidence"] < 0.5:
            issues.append("many_pitfalls_low_confidence")

        if issues:
            weak.append({
                "name": s["name"],
                "issues": issues,
                "success_rate": round(rate, 3),
                "confidence": s["confidence"],
                "maturity": s["maturity"],
                "use_count": use,
                "days_since_use": (now - s["last_used"]).days if s["last_used"] else None,
            })

    return weak


# ============================================================
# 2. Gap Detection
# ============================================================

def _normalize(text_input: str) -> str:
    """Lowercase, strip punctuation for simple matching."""
    return re.sub(r'[^a-z0-9\s]', '', text_input.lower()).strip()


def detect_gaps(tasks: list[dict], existing_skill_names: list[str]) -> list[dict]:
    """Find recurring task patterns that don't map to any existing skill.

    Uses simple keyword clustering: group unmapped tasks by shared significant words,
    flag clusters with >= GAP_MIN_OCCURRENCES.
    """
    # Filter to tasks without skills
    unmapped = [t for t in tasks if not t.get("skills_used")]
    if not unmapped:
        return []

    # Extract significant words (>3 chars, not stopwords)
    stopwords = {"this", "that", "with", "from", "have", "been", "will", "what",
                 "when", "where", "which", "there", "their", "about", "some",
                 "into", "over", "after", "before", "between", "through",
                 "the", "and", "for", "are", "but", "not", "you", "all",
                 "can", "had", "her", "was", "one", "our", "out", "new", "task"}

    skill_words = set()
    for name in existing_skill_names:
        skill_words.update(_normalize(name).split())

    # Cluster by significant words
    word_to_tasks = defaultdict(list)
    for t in unmapped:
        desc = _normalize(t["description"])
        words = [w for w in desc.split() if len(w) > 3 and w not in stopwords and w not in skill_words]
        for w in words:
            word_to_tasks[w].append(t["description"])

    # Find clusters meeting threshold
    gaps = []
    seen_words = set()
    for word, descs in sorted(word_to_tasks.items(), key=lambda x: -len(x[1])):
        if word in seen_words:
            continue
        if len(descs) >= GAP_MIN_OCCURRENCES:
            gaps.append({
                "pattern": word,
                "task_descriptions": descs[:10],
                "count": len(descs),
            })
            seen_words.add(word)

    return gaps


# ============================================================
# 3. Improvement Suggestions
# ============================================================

def suggest_improvements(skill: dict, failures: list[dict]) -> list[dict]:
    """Generate improvement suggestions from failure analysis."""
    suggestions = []

    if not failures:
        return suggestions

    # Extract common error themes
    error_texts = [f.get("error_analysis", "") or "" for f in failures]
    task_texts = [f.get("task_description", "") or "" for f in failures]

    # Simple: each failure with an error_analysis becomes a suggestion
    for f in failures:
        err = f.get("error_analysis") or f.get("task_description", "")
        if err:
            suggestions.append({
                "type": "add_guardrail",
                "skill": skill["name"],
                "reason": f"Failure: {err[:200]}",
                "suggested_pitfall": err[:300],
            })

    # If multiple failures, suggest procedure review
    if len(failures) >= 2:
        suggestions.append({
            "type": "procedure_review",
            "skill": skill["name"],
            "reason": f"{len(failures)} failures detected — procedure may need revision",
            "failure_count": len(failures),
        })

    return suggestions


# ============================================================
# 4. Auto-creation
# ============================================================

def propose_skill_from_gap(gap: dict) -> Optional[dict]:
    """Propose a new skill from a detected gap. Returns None if below threshold."""
    if gap["count"] < GAP_MIN_OCCURRENCES:
        return None

    pattern = gap["pattern"]
    descriptions = gap["task_descriptions"]

    return {
        "name": pattern,
        "description": f"Auto-detected skill for recurring '{pattern}' tasks. Based on {gap['count']} unmapped task occurrences.",
        "procedure": f"Emerging skill — needs manual procedure definition.\nSample tasks:\n" +
                     "\n".join(f"- {d}" for d in descriptions[:5]),
        "auto_emerged": True,
        "level": "cognitive",
    }


def create_skill_in_db(skill_data: dict) -> Optional[int]:
    """Insert an auto-emerged skill into the database.

    Enforces the centralized skill-creator gate before insertion.
    """
    from brain.systems.skills.gate import enforce_gate, SkillGateError

    try:
        enforce_gate(
            skill_data["name"], skill_data.get("description", ""),
            skill_data.get("procedure", ""),
            automated=True, raise_on_fail=True,
        )
    except SkillGateError as e:
        import logging
        logging.getLogger(__name__).warning(
            "Auto-emerged skill '%s' blocked by gate: %s", skill_data["name"], e.violations
        )
        return None

    emb_text = f"{skill_data['name']}: {skill_data['description']}"
    embedding = embed_document(emb_text)

    with UnitOfWork() as uow:
        # Check if exists
        existing = uow.session.execute(text(
            "SELECT id FROM skills WHERE name = :name"
        ), {"name": skill_data["name"]}).mappings().first()
        if existing:
            return None  # already exists

        row = uow.session.execute(text("""
            INSERT INTO skills (name, description, procedure, level, auto_emerged, embedding)
            VALUES (:name, :description, :procedure, :level, :auto_emerged, CAST(:embedding AS vector))
            RETURNING id
        """), {
            "name": skill_data["name"], "description": skill_data["description"],
            "procedure": skill_data["procedure"],
            "level": skill_data.get("level", "cognitive"),
            "auto_emerged": skill_data.get("auto_emerged", True),
            "embedding": vec_to_pg(embedding),
        }).mappings().first()
        return row["id"]


# ============================================================
# 5. Meta-metrics
# ============================================================

def compute_meta_metrics(skills: list[dict], tasks_total: int, tasks_with_skill: int) -> dict:
    """Compute aggregate metrics about the skill system itself."""
    if not skills:
        return {
            "total_skills": 0, "coverage": 0.0, "avg_success_rate": 0.0,
            "avg_maturity_score": 0.0, "avg_confidence": 0.0,
            "maturity_distribution": {},
        }

    success_rates = [s["success_count"] / max(s["use_count"], 1) for s in skills]
    maturity_scores = [MATURITY_SCORES.get(s["maturity"], 1) for s in skills]
    confidences = [s["confidence"] for s in skills]

    mat_dist = Counter(s["maturity"] for s in skills)

    return {
        "total_skills": len(skills),
        "coverage": round(tasks_with_skill / max(tasks_total, 1), 3),
        "avg_success_rate": round(sum(success_rates) / len(success_rates), 3),
        "avg_maturity_score": round(sum(maturity_scores) / len(maturity_scores), 3),
        "avg_confidence": round(sum(confidences) / len(confidences), 3),
        "maturity_distribution": dict(mat_dist),
        "active_skills": sum(1 for s in skills if s["use_count"] > 0),
        "dormant_skills": sum(1 for s in skills if s["use_count"] == 0),
    }


# ============================================================
# Full Analysis (DB-connected)
# ============================================================

def run_full_analysis(days: int = 7) -> dict:
    """Run the complete meta-skill analysis against the database."""
    with UnitOfWork() as uow:
        # Load all skills
        skill_rows = uow.session.execute(text("""
            SELECT id, name, description, maturity, confidence, use_count,
                   success_count, failure_count, partial_count, pitfalls,
                   last_used, auto_emerged
            FROM skills WHERE NOT archived
        """)).mappings().all()
        skills = [dict(r) for r in skill_rows]
        skill_names = [s["name"] for s in skills]

        # Load recent tasks
        task_rows = uow.session.execute(text("""
            SELECT description, task_type, skills_used
            FROM tasks
            WHERE created_at >= CURRENT_DATE - INTERVAL :days_interval
        """), {"days_interval": f"{days} days"}).mappings().all()
        tasks = [dict(r) for r in task_rows]

        # Task coverage counts
        tasks_total = len(tasks)
        tasks_with_skill = sum(1 for t in tasks if t.get("skills_used"))

        # Load failure executions per skill
        skill_failures = {}
        for s in skills:
            if s["failure_count"] > 0:
                fail_rows = uow.session.execute(text("""
                    SELECT task_description, error_analysis, outcome
                    FROM skill_executions
                    WHERE skill_id = :skill_id AND outcome = 'failure'
                    ORDER BY started_at DESC LIMIT 10
                """), {"skill_id": s["id"]}).mappings().all()
                skill_failures[s["name"]] = [dict(r) for r in fail_rows]

    # Run analyses
    weaknesses = analyze_weaknesses(skills)
    gaps = detect_gaps(tasks, skill_names)
    meta_metrics = compute_meta_metrics(skills, tasks_total, tasks_with_skill)

    # Generate improvement suggestions for weak skills
    improvements = []
    for w in weaknesses:
        if "low_success_rate" in w["issues"]:
            failures = skill_failures.get(w["name"], [])
            improvements.extend(suggest_improvements(
                {"name": w["name"]}, failures
            ))

    # Propose auto-creations
    proposed_skills = []
    for gap in gaps:
        proposal = propose_skill_from_gap(gap)
        if proposal:
            proposed_skills.append(proposal)

    return {
        "timestamp": datetime.now().isoformat(),
        "period_days": days,
        "meta_metrics": meta_metrics,
        "weaknesses": weaknesses,
        "gaps": gaps,
        "improvements": improvements,
        "proposed_skills": proposed_skills,
        "skill_health": [{
            "name": s["name"],
            "maturity": s["maturity"],
            "confidence": s["confidence"],
            "success_rate": round(s["success_count"] / max(s["use_count"], 1), 3),
            "use_count": s["use_count"],
            "auto_emerged": s.get("auto_emerged", False),
        } for s in skills],
    }


def run_auto_create(days: int = 7, dry_run: bool = False) -> list[dict]:
    """Detect gaps and auto-create skills."""
    with UnitOfWork() as uow:
        name_rows = uow.session.execute(text(
            "SELECT name FROM skills WHERE NOT archived"
        )).mappings().all()
        skill_names = [r["name"] for r in name_rows]

        task_rows = uow.session.execute(text("""
            SELECT description, task_type, skills_used
            FROM tasks WHERE created_at >= CURRENT_DATE - INTERVAL :days_interval
        """), {"days_interval": f"{days} days"}).mappings().all()
        tasks = [dict(r) for r in task_rows]

    gaps = detect_gaps(tasks, skill_names)
    created = []

    for gap in gaps:
        proposal = propose_skill_from_gap(gap)
        if proposal:
            if dry_run:
                created.append({"proposed": proposal, "created": False})
            else:
                skill_id = create_skill_in_db(proposal)
                created.append({
                    "proposed": proposal,
                    "created": skill_id is not None,
                    "skill_id": skill_id,
                })

    return created


# ============================================================
# Nightly Integration
# ============================================================

def nightly_meta_analysis() -> str:
    """Run meta-analysis as part of nightly cycle. Returns summary string."""
    analysis = run_full_analysis(days=7)

    lines = ["## Meta-Skill Analysis"]
    m = analysis["meta_metrics"]
    lines.append(f"- **Skills:** {m['total_skills']} total, {m.get('active_skills', 0)} active, {m.get('dormant_skills', 0)} dormant")
    lines.append(f"- **Coverage:** {m['coverage']*100:.0f}% of tasks mapped to skills")
    lines.append(f"- **Avg success rate:** {m['avg_success_rate']*100:.0f}%")
    lines.append(f"- **Avg confidence:** {m['avg_confidence']:.2f}")
    lines.append(f"- **Maturity distribution:** {m['maturity_distribution']}")

    if analysis["weaknesses"]:
        lines.append("\n### Weak Skills")
        for w in analysis["weaknesses"]:
            lines.append(f"- **{w['name']}**: {', '.join(w['issues'])} (sr={w['success_rate']:.0%}, conf={w['confidence']:.2f})")

    if analysis["gaps"]:
        lines.append("\n### Skill Gaps")
        for g in analysis["gaps"]:
            lines.append(f"- **{g['pattern']}**: {g['count']} unmapped tasks")

    if analysis["improvements"]:
        lines.append("\n### Suggested Improvements")
        for imp in analysis["improvements"][:5]:
            lines.append(f"- [{imp['type']}] {imp['skill']}: {imp['reason'][:100]}")

    if analysis["proposed_skills"]:
        lines.append("\n### Proposed New Skills")
        for p in analysis["proposed_skills"]:
            lines.append(f"- **{p['name']}**: {p['description'][:100]}")

    return "\n".join(lines)


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Illo Meta-Skill Analyzer")
    subparsers = parser.add_subparsers(dest="command")

    p_analyze = subparsers.add_parser("analyze", help="Full meta-skill analysis")
    p_analyze.add_argument("--days", type=int, default=7)

    p_weak = subparsers.add_parser("weaknesses", help="Weakness report")
    p_gaps = subparsers.add_parser("gaps", help="Gap detection")
    p_gaps.add_argument("--days", type=int, default=7)

    p_metrics = subparsers.add_parser("metrics", help="Meta-metrics")
    p_metrics.add_argument("--days", type=int, default=7)

    p_auto = subparsers.add_parser("auto-create", help="Auto-create skills from gaps")
    p_auto.add_argument("--days", type=int, default=7)
    p_auto.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()

    if args.command == "analyze":
        result = run_full_analysis(args.days)
        print(json.dumps(result, indent=2, default=str))

    elif args.command == "weaknesses":
        with UnitOfWork() as uow:
            rows = uow.session.execute(text(
                "SELECT name, maturity, confidence, use_count, success_count, failure_count, partial_count, pitfalls, last_used FROM skills WHERE NOT archived"
            )).mappings().all()
            skills = [dict(r) for r in rows]
        result = analyze_weaknesses(skills)
        print(json.dumps(result, indent=2, default=str))

    elif args.command == "gaps":
        with UnitOfWork() as uow:
            name_rows = uow.session.execute(text(
                "SELECT name FROM skills WHERE NOT archived"
            )).mappings().all()
            skill_names = [r["name"] for r in name_rows]
            task_rows = uow.session.execute(text(
                "SELECT description, skills_used FROM tasks WHERE created_at >= CURRENT_DATE - INTERVAL :days_interval"
            ), {"days_interval": f"{args.days} days"}).mappings().all()
            tasks = [dict(r) for r in task_rows]
        result = detect_gaps(tasks, skill_names)
        print(json.dumps(result, indent=2, default=str))

    elif args.command == "auto-create":
        result = run_auto_create(args.days, args.dry_run)
        print(json.dumps(result, indent=2, default=str))

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
