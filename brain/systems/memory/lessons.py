"""Lesson Compiler — turns passive lessons into active guardian rules.

Runs during nightly cycle (Phase 3.5) and on-demand for initial compilation.
"""

import json
import os
import re
import sys
from pathlib import Path
from datetime import date, datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))

from sqlalchemy import text

import brain.kernel.config as config
from brain.app.cli.agent_cli import call_agent, extract_json
from brain.platform.db.repositories.unit_of_work import UnitOfWork, open_unit_of_work
from brain.platform.providers.model_policy import get_model_for_tier


def audit_lessons(target_date: date) -> dict:
    """Analyze today's lessons for repeats, violations, and new rule candidates."""
    with open_unit_of_work(UnitOfWork) as uow:
        # Get today's lessons
        rows = uow.session.execute(text("""
            SELECT id, content, salience, tags
            FROM memories WHERE memory_type = 'lesson'
            AND created_at::date = :target_date AND NOT archived
            ORDER BY salience DESC
        """), {"target_date": target_date}).mappings().all()
        todays_lessons = [dict(r) for r in rows]

        # Get existing rules
        rule_rows = uow.session.execute(text(
            "SELECT id, name, source_lesson_ids FROM guardian_rules WHERE active = true"
        )).mappings().all()
        existing_rules = [dict(r) for r in rule_rows]

        # Get all existing lesson IDs that are already compiled
        compiled_ids = set()
        for rule in existing_rules:
            if rule["source_lesson_ids"]:
                compiled_ids.update(rule["source_lesson_ids"])

    report = {
        "date": str(target_date),
        "total_lessons_today": len(todays_lessons),
        "already_compiled": [],
        "new_compilable": [],
        "low_salience_skipped": [],
        "violations": [],
    }

    for lesson in todays_lessons:
        if lesson["id"] in compiled_ids:
            report["already_compiled"].append(lesson["id"])
            # This is a repeat/violation — log it
            for rule in existing_rules:
                if rule["source_lesson_ids"] and lesson["id"] in rule["source_lesson_ids"]:
                    report["violations"].append({
                        "lesson_id": lesson["id"],
                        "rule_id": rule["id"],
                        "rule_name": rule["name"],
                    })
        elif lesson["salience"] and lesson["salience"] >= 7:
            report["new_compilable"].append(lesson["id"])
        else:
            report["low_salience_skipped"].append(lesson["id"])

    # Check for semantic repeats (lesson content similar to existing rules)
    # We use simple keyword matching here; semantic similarity would require embeddings
    for lesson in todays_lessons:
        if lesson["id"] not in compiled_ids:
            content_lower = lesson["content"].lower()
            for rule in existing_rules:
                rule_name_lower = rule["name"].lower()
                # Simple overlap check
                if any(word in content_lower for word in rule_name_lower.split("_") if len(word) > 4):
                    if lesson["id"] not in [v["lesson_id"] for v in report["violations"]]:
                        report["violations"].append({
                            "lesson_id": lesson["id"],
                            "rule_id": rule["id"],
                            "rule_name": rule["name"],
                            "type": "semantic_repeat",
                        })

    # Log violations to DB
    for v in report["violations"]:
        with open_unit_of_work(UnitOfWork) as uow:
            uow.session.execute(text("""
                INSERT INTO violation_log (lesson_id, guardian_rule_id, detected_by, context, session_date)
                VALUES (:lesson_id, :rule_id, 'nightly_audit', :context, :session_date)
            """), {
                "lesson_id": v["lesson_id"], "rule_id": v["rule_id"],
                "context": f"Lesson repeated existing rule: {v['rule_name']}",
                "session_date": target_date,
            })
            # Increment violation count on the rule
            uow.session.execute(text("""
                UPDATE guardian_rules SET source_violation_count = source_violation_count + 1,
                       updated_at = NOW()
                WHERE id = :rule_id
            """), {"rule_id": v["rule_id"]})

    return report


def compile_lesson_to_rule(lesson_id: int) -> int | None:
    """Compile a lesson into a guardian rule using LLM analysis."""
    with open_unit_of_work(UnitOfWork) as uow:
        row = uow.session.execute(text(
            "SELECT id, content, salience, tags FROM memories WHERE id = :id"
        ), {"id": lesson_id}).mappings().first()
        lesson = dict(row) if row else None

    if not lesson:
        return None

    prompt = f"""Analyze this lesson and determine if it can be compiled into an enforcement rule.

LESSON (salience {lesson['salience']}/10):
{lesson['content'][:500]}

Return a JSON object with:
- "compilable": true/false — can this be turned into a checkable rule?
- "name": short snake_case name (max 50 chars), e.g. "verify_before_done"
- "description": one-line description
- "trigger_type": "pre_completion" (check before presenting work) | "pre_action" (check before specific actions) | "pattern_match" (check for behavioral patterns)
- "trigger_pattern": JSON object with context keys like {{"requires_code": true}} or {{"keywords": ["deploy", "ship"]}}
- "required_evidence": array of evidence strings to look for in action log, e.g. ["test_execution", "pytest", "test output"]
- "check_description": human-readable instruction, e.g. "Run tests and verify output before presenting code changes"
- "category": "code" | "investigation" | "process" | "communication" | "delegation"
- "priority": 1-10 (1=critical, 10=nice-to-have)

Return ONLY the JSON object, no other text."""

    result = call_agent(
        session_id=f"lesson-compile-{lesson_id}",
        message=prompt,
        model=get_model_for_tier("low", include_provider_prefix=True),
        thinking="off",
    )

    if not result["success"]:
        return None

    parsed = extract_json(result["text"])
    if not parsed or not parsed.get("compilable"):
        return None

    # Insert the rule
    with open_unit_of_work(UnitOfWork) as uow:
        # Check for duplicate name
        existing = uow.session.execute(text(
            "SELECT id FROM guardian_rules WHERE name = :name"
        ), {"name": parsed["name"]}).mappings().first()
        if existing:
            # Update existing rule's source lessons
            row = uow.session.execute(text("""
                UPDATE guardian_rules
                SET source_lesson_ids = array_append(
                    COALESCE(source_lesson_ids, ARRAY[]::int[]), :lesson_id
                ), updated_at = NOW()
                WHERE name = :name RETURNING id
            """), {"lesson_id": lesson_id, "name": parsed["name"]}).mappings().first()
            return row["id"]

        row = uow.session.execute(text("""
            INSERT INTO guardian_rules
                (name, description, trigger_type, trigger_pattern,
                 required_evidence, check_description, source_lesson_ids,
                 trust_level_required)
            VALUES (:name, :description, :trigger_type, :trigger_pattern,
                    :required_evidence, :check_description, :source_lesson_ids,
                    :trust_level_required)
            RETURNING id
        """), {
            "name": parsed["name"],
            "description": parsed.get("description", ""),
            "trigger_type": parsed.get("trigger_type", "pre_completion"),
            "trigger_pattern": json.dumps(parsed.get("trigger_pattern", {})),
            "required_evidence": parsed.get("required_evidence", []),
            "check_description": parsed.get("check_description", ""),
            "source_lesson_ids": [lesson_id],
            "trust_level_required": 0,
        }).mappings().first()
        rule_id = row["id"]

        # Also create checklist item
        uow.session.execute(text("""
            INSERT INTO checklist_items (category, check_text, source_rule_id, priority)
            VALUES (:category, :check_text, :source_rule_id, :priority)
        """), {
            "category": parsed.get("category", "process"),
            "check_text": parsed.get("check_description", ""),
            "source_rule_id": rule_id,
            "priority": parsed.get("priority", 5),
        })

    return rule_id


def escalate_rule(rule_id: int):
    """Make a rule stricter when it keeps getting violated."""
    with open_unit_of_work(UnitOfWork) as uow:
        uow.session.execute(text("""
            UPDATE guardian_rules SET
                trust_level_required = GREATEST(0, trust_level_required - 1),
                updated_at = NOW()
            WHERE id = :rule_id
        """), {"rule_id": rule_id})
        # Bump checklist priority
        uow.session.execute(text("""
            UPDATE checklist_items SET
                priority = GREATEST(1, priority - 1)
            WHERE source_rule_id = :rule_id
        """), {"rule_id": rule_id})


def generate_checklist():
    """Regenerate checklist_items and persist the public checklist markdown.

    The checklist is runtime-private operator context. It no longer edits root
    prompt files, which makes the repository safe to publish without shipping
    personalized agent prompts.
    """
    with open_unit_of_work(UnitOfWork) as uow:
        # Clear old checklist items and regenerate from rules
        uow.session.execute(text("DELETE FROM checklist_items"))
        rows = uow.session.execute(text("""
            SELECT id, name, check_description, trigger_type, trigger_pattern
            FROM guardian_rules WHERE active = true
            ORDER BY times_bounced DESC, source_violation_count DESC
        """)).mappings().all()
        rules = [dict(r) for r in rows]

    for rule in rules:
        # Determine category from trigger_pattern
        pattern = rule.get("trigger_pattern") or {}
        if pattern.get("requires_code"):
            category = "code"
        elif pattern.get("requires_investigation"):
            category = "investigation"
        elif pattern.get("keywords") and any(
            k in ["delegate", "child agent", "spawn"] for k in pattern.get("keywords", [])
        ):
            category = "delegation"
        else:
            category = "process"

        # Priority: more violations = higher priority
        with open_unit_of_work(UnitOfWork) as uow:
            stats = uow.session.execute(text(
                "SELECT source_violation_count, times_bounced FROM guardian_rules WHERE id = :id"
            ), {"id": rule["id"]}).mappings().first()
            violations = (stats["source_violation_count"] or 0) + (stats["times_bounced"] or 0)
            priority = max(1, 5 - violations)  # More violations -> lower number -> higher priority

            uow.session.execute(text("""
                INSERT INTO checklist_items (category, check_text, source_rule_id, priority)
                VALUES (:category, :check_text, :source_rule_id, :priority)
            """), {
                "category": category,
                "check_text": rule["check_description"],
                "source_rule_id": rule["id"],
                "priority": priority,
            })

    from brain.systems.quality.guardian import get_scout_checklist
    checklist_md = get_scout_checklist()
    _write_agent_checklist(checklist_md)


def _write_agent_checklist(checklist_md: str) -> Path:
    """Write generated guardian checklist to the configured private context path."""
    checklist_path = config.AGENT_CHECKLIST_PATH
    checklist_path.parent.mkdir(parents=True, exist_ok=True)
    checklist_path.write_text(checklist_md.rstrip() + "\n", encoding="utf-8")
    return checklist_path


def compile_all_high_salience(min_salience: float = 7.0) -> dict:
    """Compile all uncompiled high-salience lessons into rules."""
    with open_unit_of_work(UnitOfWork) as uow:
        # Get already-compiled lesson IDs
        rule_rows = uow.session.execute(text(
            "SELECT source_lesson_ids FROM guardian_rules WHERE source_lesson_ids IS NOT NULL"
        )).mappings().all()
        compiled_ids = set()
        for row in rule_rows:
            if row["source_lesson_ids"]:
                compiled_ids.update(row["source_lesson_ids"])

        # Get uncompiled high-salience lessons
        lesson_rows = uow.session.execute(text("""
            SELECT id, content, salience FROM memories
            WHERE memory_type = 'lesson' AND salience >= :min_salience AND NOT archived
            ORDER BY salience DESC
        """), {"min_salience": min_salience}).mappings().all()
        lessons = [dict(r) for r in lesson_rows]

    results = {"compiled": [], "skipped": [], "failed": []}

    for lesson in lessons:
        if lesson["id"] in compiled_ids:
            results["skipped"].append({"id": lesson["id"], "reason": "already compiled"})
            continue

        rule_id = compile_lesson_to_rule(lesson["id"])
        if rule_id:
            results["compiled"].append({"lesson_id": lesson["id"], "rule_id": rule_id})
        else:
            results["failed"].append({"lesson_id": lesson["id"]})

    # Regenerate checklist after compilation
    if results["compiled"]:
        generate_checklist()

    return results
