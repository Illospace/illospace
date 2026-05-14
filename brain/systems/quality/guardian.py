"""Guardian — the cognitive enforcement layer.

Loads compiled rules from the brain and checks agent completions
against them before presenting work to the user.
"""

from datetime import datetime

from sqlalchemy import text

from brain.platform.db.repositories.unit_of_work import UnitOfWork


async def load_rules(active_only: bool = True) -> list[dict]:
    """Load guardian rules from DB."""
    async with UnitOfWork() as uow:
        clause = "WHERE active = true" if active_only else ""
        rows = (await uow.session.execute(text(f"""
            SELECT id, name, description, trigger_type, trigger_pattern,
                   required_evidence, check_description, source_lesson_ids,
                   source_violation_count, trust_level_required,
                   times_enforced, times_passed, times_bounced
            FROM guardian_rules {clause}
            ORDER BY trust_level_required ASC, times_bounced DESC
        """))).mappings().all()
        return [dict(r) for r in rows]


async def check_completion(action_log: list[str], task_context: dict) -> tuple[bool, list[str]]:
    """Check if a completion should be allowed through.

    Returns (allowed, violations).
    action_log: list of recent agent actions (tool calls, file edits, etc.)
    task_context: {involves_code: bool, involves_investigation: bool, ...}
    """
    rules = await load_rules()
    trust = await get_trust_level()
    violations = []
    action_text = " ".join(action_log).lower()

    for rule in rules:
        # Skip rules below current trust level
        if rule["trust_level_required"] > 0 and trust["current_level"] >= rule["trust_level_required"]:
            continue

        # Check if rule is relevant to this task
        trigger = rule["trigger_type"]
        if trigger == "pre_completion":
            relevant = True
        elif trigger == "pre_action":
            # Check trigger_pattern for context match
            pattern = rule.get("trigger_pattern") or {}
            if pattern.get("requires_code") and not task_context.get("involves_code"):
                relevant = False
            elif pattern.get("requires_investigation") and not task_context.get("involves_investigation"):
                relevant = False
            else:
                relevant = True
        elif trigger == "pattern_match":
            pattern = rule.get("trigger_pattern") or {}
            keywords = pattern.get("keywords", [])
            relevant = any(kw.lower() in action_text for kw in keywords) if keywords else False
        else:
            relevant = True

        if not relevant:
            continue

        # Check required evidence
        missing = []
        for evidence in rule["required_evidence"]:
            if evidence.lower() not in action_text:
                missing.append(evidence)

        # Update enforcement counter
        async with UnitOfWork() as uow:
            await uow.session.execute(text("""
                UPDATE guardian_rules SET times_enforced = times_enforced + 1,
                       updated_at = NOW() WHERE id = :id
            """), {"id": rule["id"]})

        if missing:
            violations.append(
                f"[{rule['name']}] {rule['check_description']} "
                f"(missing: {', '.join(missing)})"
            )
            async with UnitOfWork() as uow:
                await uow.session.execute(text("""
                    UPDATE guardian_rules SET times_bounced = times_bounced + 1
                    WHERE id = :id
                """), {"id": rule["id"]})
        else:
            async with UnitOfWork() as uow:
                await uow.session.execute(text("""
                    UPDATE guardian_rules SET times_passed = times_passed + 1
                    WHERE id = :id
                """), {"id": rule["id"]})

    return (len(violations) == 0, violations)


async def record_completion(passed: bool, violations: list[str], caught_by: str):
    """Record a completion outcome and update trust state.

    caught_by: 'guardian', 'self', 'user'
    """
    async with UnitOfWork() as uow:
        if caught_by == "user":
            # Worst case: user caught something we missed
            await uow.session.execute(text("""
                UPDATE trust_state SET
                    total_completions = total_completions + 1,
                    total_user_caught = total_user_caught + 1,
                    consecutive_clean = 0,
                    current_level = GREATEST(0, current_level - 1),
                    last_demotion_reason = :reason,
                    updated_at = NOW()
                WHERE id = (SELECT id FROM trust_state LIMIT 1)
            """), {"reason": f"User caught: {'; '.join(violations[:3])}"})
        elif caught_by == "guardian":
            await uow.session.execute(text("""
                UPDATE trust_state SET
                    total_completions = total_completions + 1,
                    total_bounced = total_bounced + 1,
                    consecutive_clean = 0,
                    updated_at = NOW()
                WHERE id = (SELECT id FROM trust_state LIMIT 1)
            """))
        else:
            # Clean pass or self-corrected
            await uow.session.execute(text("""
                UPDATE trust_state SET
                    total_completions = total_completions + 1,
                    consecutive_clean = consecutive_clean + 1,
                    current_level = CASE
                        WHEN consecutive_clean + 1 >= level_up_threshold AND current_level < 3
                        THEN current_level + 1
                        ELSE current_level
                    END,
                    consecutive_clean = CASE
                        WHEN consecutive_clean + 1 >= level_up_threshold AND current_level < 3
                        THEN 0
                        ELSE consecutive_clean + 1
                    END,
                    updated_at = NOW()
                WHERE id = (SELECT id FROM trust_state LIMIT 1)
            """))

        # Log violations
        for v in violations:
            await uow.session.execute(text("""
                INSERT INTO violation_log (detected_by, context, session_date)
                VALUES (:detected_by, :context, CURRENT_DATE)
            """), {"detected_by": caught_by, "context": v})


async def get_trust_level() -> dict:
    """Return current trust state."""
    async with UnitOfWork() as uow:
        row = (await uow.session.execute(
            text("SELECT * FROM trust_state LIMIT 1")
        )).mappings().first()
        if not row:
            return {"current_level": 0, "consecutive_clean": 0,
                    "total_completions": 0, "level_name": "probation"}

        level_names = {0: "probation", 1: "supervised", 2: "trusted", 3: "autonomous"}
        result = dict(row)
        result["level_name"] = level_names.get(result["current_level"], "unknown")
        return result


async def demote(reason: str):
    """Demote trust level. Called when user catches a miss."""
    async with UnitOfWork() as uow:
        await uow.session.execute(text("""
            UPDATE trust_state SET
                current_level = GREATEST(0, current_level - 1),
                consecutive_clean = 0,
                last_demotion_reason = :reason,
                updated_at = NOW()
            WHERE id = (SELECT id FROM trust_state LIMIT 1)
        """), {"reason": reason})


async def get_scout_checklist() -> str:
    """Generate the pre-flight checklist as markdown for private operator context."""
    async with UnitOfWork() as uow:
        rows = (await uow.session.execute(text("""
            SELECT category, check_text, priority
            FROM checklist_items WHERE active = true
            ORDER BY priority ASC, category, check_text
        """))).mappings().all()
        items = [dict(r) for r in rows]

    if not items:
        return "## Pre-Flight Checklist\n\n_No checklist items yet. Rules will be compiled from lessons._\n"

    md = "## Pre-Flight Checklist\n\n"
    md += "_Before presenting work, verify:_\n\n"

    # Group by category
    categories: dict[str, list] = {}
    for item in items:
        cat = item["category"]
        if cat not in categories:
            categories[cat] = []
        categories[cat].append(item)

    for cat, cat_items in categories.items():
        md += f"### {cat.title()}\n"
        for item in cat_items:
            priority_marker = "🔴" if item["priority"] <= 2 else "🟡" if item["priority"] <= 5 else "⚪"
            md += f"- {priority_marker} {item['check_text']}\n"
        md += "\n"

    return md
