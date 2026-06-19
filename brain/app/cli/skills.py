#!/usr/bin/env python3
"""
Illo Skill System — Living procedures that evolve with use.

Usage:
    skills.py list                                    # show all skills with metrics
    skills.py get <name>                              # full skill details + procedure
    skills.py create --name debug --desc "..." --procedure "..."  # create new skill
    skills.py use <name> --task "..." [--outcome success] [--duration 300]
    skills.py refine <name> --change "..." --reason "..."
    skills.py pitfall <name> --text "..." [--severity high]
    skills.py depend --parent develop --child test --rel requires
    skills.py plan "task description"                 # task router — generates execution plan
    skills.py feedback <execution_id> --outcome success [--notes "..."]
    skills.py dashboard                               # full performance dashboard
    skills.py evolve                                  # run skill evolution (nightly)
"""

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timedelta, date

import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))  # repo root

import brain.kernel.config as config
from brain.platform.db.repositories.unit_of_work import UnitOfWork
from brain.platform.db.models.skill import Skill, SkillDependency, SkillExecution
from brain.systems.memory.embeddings import embed_batch, embed_document, embed_query, vec_to_pg

from sqlalchemy import func, select, text

_vec_to_pg = vec_to_pg

# ============================================================
# Maturity Calculation
# ============================================================

def compute_maturity(use_count: int, success_rate: float) -> tuple:
    """Compute skill maturity level and confidence score."""
    if use_count < 3:
        maturity = "emerging"
        confidence = min(0.3, success_rate * 0.3)
    elif use_count < 10:
        maturity = "developing"
        confidence = min(0.6, success_rate * 0.5 + use_count / 30)
    elif use_count < 25 and success_rate >= 0.7:
        maturity = "proficient"
        confidence = min(0.85, success_rate * 0.6 + use_count / 50)
    elif use_count >= 25 and success_rate >= 0.85:
        maturity = "expert"
        confidence = min(1.0, success_rate * 0.7 + use_count / 100)
    else:
        maturity = "developing"
        confidence = min(0.5, success_rate * 0.4 + use_count / 40)

    return maturity, round(confidence, 3)


# ============================================================
# Commands
# ============================================================

async def cmd_list(args):
    """List all skills with metrics."""
    async with UnitOfWork() as uow:
        # skill_dashboard is a DB view — use raw SQL for it
        rows = (await uow.session.execute(text("SELECT * FROM skill_dashboard"))).mappings().all()

    output = [{
        "id": s["id"],
        "name": s["name"],
        "maturity": s["maturity"],
        "confidence": float(s["confidence"]) if s["confidence"] else 0,
        "uses": s["use_count"],
        "success_pct": float(s["success_pct"]) if s["success_pct"] else 0,
        "version": s["version"],
        "pitfalls": s["pitfall_count"],
        "sub_skills": s["sub_skill_count"],
        "last_used": s["last_used"].isoformat() if s["last_used"] else None,
    } for s in rows]

    print(json.dumps(output, indent=2, default=str))


async def cmd_get(args):
    """Get full skill details."""
    async with UnitOfWork() as uow:
        skill = await uow.skills.get_by_name(args.name)

        if not skill:
            print(json.dumps({"error": f"Skill '{args.name}' not found"}))
            return

        # Get sub-skills
        stmt = (
            select(
                Skill.name,
                SkillDependency.relationship,
                SkillDependency.execution_order,
                SkillDependency.strength,
            )
            .join(Skill, Skill.id == SkillDependency.child_id)
            .where(SkillDependency.parent_id == skill.id)
            .order_by(SkillDependency.execution_order.nulls_last())
        )
        sub_skills = (await uow.session.execute(stmt)).mappings().all()

        # Get parent skills (skills that use this one)
        stmt = (
            select(Skill.name, SkillDependency.relationship)
            .join(Skill, Skill.id == SkillDependency.parent_id)
            .where(SkillDependency.child_id == skill.id)
        )
        parent_skills = (await uow.session.execute(stmt)).mappings().all()

        # Get recent executions
        stmt = (
            select(SkillExecution)
            .where(SkillExecution.skill_id == skill.id)
            .order_by(SkillExecution.started_at.desc())
            .limit(10)
        )
        executions = (await uow.session.scalars(stmt)).all()

        result = {
            "id": skill.id,
            "name": skill.name,
            "description": skill.description,
            "level": skill.level,
            "maturity": skill.maturity,
            "confidence": float(skill.confidence) if skill.confidence else 0,
            "version": skill.version,
            "procedure": skill.procedure,
            "metrics": {
                "uses": skill.use_count,
                "successes": skill.success_count,
                "failures": skill.failure_count,
                "partial": skill.partial_count,
                "success_rate": round(skill.success_count / max(skill.use_count, 1), 3),
                "avg_duration_sec": skill.avg_duration_sec,
            },
            "pitfalls": skill.pitfalls,
            "refinements": skill.refinements,
            "triggers": skill.triggers,
            "sub_skills": [dict(s) for s in sub_skills],
            "used_by": [dict(p) for p in parent_skills],
            "recent_executions": [{
                "id": e.id,
                "task": e.task_description[:100] if e.task_description else None,
                "outcome": e.outcome,
                "duration": e.duration_sec,
                "date": e.started_at.isoformat() if e.started_at else None,
            } for e in executions],
            "auto_emerged": skill.auto_emerged,
            "created": skill.created_at.isoformat(),
        }

    print(json.dumps(result, indent=2, default=str))


def _check_skill_creator_gate(args, action_name="create"):
    """Enforce mandatory skill-creator consultation before any skill authoring.

    This is a hard gate — not advisory. If --skill-creator-ack is not passed,
    the command fails with instructions to read skill-creator first.

    Delegates to the centralized gate in core/skill_gate.py (issue #187).
    """
    from brain.systems.skills.gate import enforce_cli_gate
    passed, error_payload = enforce_cli_gate(getattr(args, 'skill_creator_ack', False))
    if not passed:
        error_payload["action"] = action_name
        print(json.dumps(error_payload))
        return False
    return True


async def cmd_create(args):
    """Create a new skill."""
    if not _check_skill_creator_gate(args, "create"):
        return

    # Generate embedding for skill matching
    emb_text = f"{args.name}: {args.desc or ''} {args.procedure[:500]}"
    embedding = embed_document(emb_text)

    async with UnitOfWork() as uow:
        skill = Skill(
            name=args.name,
            description=args.desc,
            procedure=args.procedure,
            level=args.level or "cognitive",
        )
        uow.session.add(skill)
        await uow.session.flush()

        # Set embedding via raw SQL (pgvector cast)
        await uow.session.execute(
            text("UPDATE skills SET embedding = CAST(:emb AS vector) WHERE id = :id"),
            {"emb": _vec_to_pg(embedding), "id": skill.id},
        )
        skill_id = skill.id

    print(json.dumps({"id": skill_id, "name": args.name, "maturity": "emerging"}))


async def cmd_use(args):
    """Record a skill execution."""
    async with UnitOfWork() as uow:
        skill = await uow.skills.get_by_name(args.name)
        if not skill:
            print(json.dumps({"error": f"Skill '{args.name}' not found"}))
            return

        # Record execution
        execution = SkillExecution(
            skill_id=skill.id,
            task_description=args.task,
            task_type=args.task_type,
            complexity=args.complexity,
            outcome=args.outcome or "success",
            duration_sec=args.duration,
            outcome_details=args.details,
        )
        uow.session.add(execution)
        await uow.session.flush()
        exec_id = execution.id

        # Update skill metrics
        new_use = skill.use_count + 1
        new_success = skill.success_count + (1 if args.outcome == "success" else 0)
        new_failure = skill.failure_count + (1 if args.outcome == "failure" else 0)
        new_partial = skill.partial_count + (1 if args.outcome == "partial" else 0)

        # Update average duration
        old_avg = skill.avg_duration_sec or 0
        new_avg = ((old_avg * skill.use_count) + (args.duration or 0)) / new_use if args.duration else old_avg

        # Compute new maturity
        success_rate = new_success / max(new_use, 1)
        maturity, confidence = compute_maturity(new_use, success_rate)

        skill.use_count = new_use
        skill.success_count = new_success
        skill.failure_count = new_failure
        skill.partial_count = new_partial
        skill.avg_duration_sec = new_avg
        skill.maturity = maturity
        skill.confidence = confidence
        skill.last_used = datetime.now()
        skill.updated_at = datetime.now()

    # Post-task auto-assessment via guardian
    guardian_violations = []
    needs_review = False
    try:
        from brain.systems.quality.guardian import check_completion
        action_log = [args.task or "", args.outcome or "success", args.details or ""]
        task_context = {
            "involves_code": args.task_type in ("code", "bug_fix", "feature") if args.task_type else False,
            "involves_investigation": args.task_type == "investigation" if args.task_type else False,
        }
        allowed, violations = check_completion(action_log, task_context)
        guardian_violations = violations
        if violations:
            # Critical violations → flag for review
            needs_review = True
    except Exception:
        pass  # Guardian unavailable — don't block skill recording

    result = {
        "execution_id": exec_id,
        "skill": args.name,
        "outcome": args.outcome or "success",
        "new_maturity": maturity,
        "new_confidence": confidence,
        "success_rate": round(success_rate, 3),
        "total_uses": new_use,
    }
    if guardian_violations:
        result["guardian_violations"] = guardian_violations
        result["needs_review"] = needs_review
    print(json.dumps(result))


async def cmd_refine(args):
    """Add a refinement to a skill's procedure."""
    if not _check_skill_creator_gate(args, "refine"):
        return

    async with UnitOfWork() as uow:
        skill = await uow.skills.get_by_name(args.name)
        if not skill:
            print(json.dumps({"error": f"Skill '{args.name}' not found"}))
            return

        new_version = skill.version + 1
        refinements = list(skill.refinements or [])
        refinements.append({
            "version": new_version,
            "change": args.change,
            "reason": args.reason,
            "date": datetime.now().isoformat()
        })

        # Update procedure if new one provided
        new_procedure = args.new_procedure or skill.procedure

        # Re-embed
        emb_text = f"{args.name}: {new_procedure[:500]}"
        embedding = embed_document(emb_text)

        skill.version = new_version
        skill.procedure = new_procedure
        skill.refinements = refinements
        skill.updated_at = datetime.now()
        await uow.session.flush()

        # Set embedding via raw SQL (pgvector cast)
        await uow.session.execute(
            text("UPDATE skills SET embedding = CAST(:emb AS vector) WHERE id = :id"),
            {"emb": _vec_to_pg(embedding), "id": skill.id},
        )

    print(json.dumps({"skill": args.name, "new_version": new_version, "change": args.change}))


async def cmd_pitfall(args):
    """Add a pitfall to a skill."""
    async with UnitOfWork() as uow:
        skill = await uow.skills.get_by_name(args.name)
        if not skill:
            print(json.dumps({"error": f"Skill '{args.name}' not found"}))
            return

        pitfalls = list(skill.pitfalls or [])
        pitfalls.append({
            "text": args.text,
            "severity": args.severity or "medium",
            "date": datetime.now().isoformat()
        })

        skill.pitfalls = pitfalls
        skill.updated_at = datetime.now()

    print(json.dumps({"skill": args.name, "pitfall_added": args.text, "total_pitfalls": len(pitfalls)}))


async def cmd_depend(args):
    """Create a dependency between skills."""
    async with UnitOfWork() as uow:
        parent = await uow.skills.get_by_name(args.parent)
        child = await uow.skills.get_by_name(args.child)

        if not parent or not child:
            print(json.dumps({"error": "One or both skills not found"}))
            return

        # Use raw SQL for ON CONFLICT upsert
        result = await uow.session.execute(
            text("""
                INSERT INTO skill_dependencies (parent_id, child_id, relationship, execution_order, strength)
                VALUES (:parent_id, :child_id, :rel, :order, :strength)
                ON CONFLICT (parent_id, child_id) DO UPDATE SET relationship = EXCLUDED.relationship
                RETURNING id
            """),
            {
                "parent_id": parent.id,
                "child_id": child.id,
                "rel": args.rel,
                "order": args.order,
                "strength": args.strength or 1.0,
            },
        )
        dep_id = result.scalar_one()

    print(json.dumps({"dependency_id": dep_id, "parent": args.parent, "child": args.child, "rel": args.rel}))


async def cmd_plan(args):
    """Task router — analyze task and generate execution plan."""
    task_text = args.task
    task_emb = embed_query(task_text)
    emb_str = _vec_to_pg(task_emb)
    words = [w for w in task_text.lower().split() if len(w) > 3][:5]
    if words:
        conditions = " OR ".join([f"input_message ILIKE :word_{i}" for i in range(len(words))])
        score_terms = " + ".join([
            f"CASE WHEN input_message ILIKE :word_{i} THEN 1 ELSE 0 END"
            for i in range(len(words))
        ])
        similar_sql = f"""
                SELECT id,
                       input_message AS description,
                       COALESCE(metadata->>'task_type', recipe) AS task_type,
                       COALESCE(metadata->>'strategy_chosen', recipe) AS strategy_chosen,
                       COALESCE(metadata->>'outcome', status) AS outcome,
                       EXTRACT(EPOCH FROM (
                           COALESCE(completed_at, failed_at, canceled_at, updated_at, created_at) - created_at
                       ))::float AS duration_sec,
                       NULL::float AS operator_satisfaction,
                       CASE
                           WHEN jsonb_typeof(metadata->'skills_used') = 'array'
                           THEN metadata->'skills_used'
                           ELSE '[]'::jsonb
                       END AS skills_used,
                       (({score_terms})::float / :word_count) AS similarity
                FROM agent_runs
                WHERE input_message IS NOT NULL
                  AND ({conditions})
                ORDER BY similarity DESC, created_at DESC
                LIMIT 5
        """
        similar_params = {f"word_{i}": f"%{w}%" for i, w in enumerate(words)}
        similar_params["word_count"] = len(words)
    else:
        similar_sql = """
                SELECT id,
                       input_message AS description,
                       COALESCE(metadata->>'task_type', recipe) AS task_type,
                       COALESCE(metadata->>'strategy_chosen', recipe) AS strategy_chosen,
                       COALESCE(metadata->>'outcome', status) AS outcome,
                       EXTRACT(EPOCH FROM (
                           COALESCE(completed_at, failed_at, canceled_at, updated_at, created_at) - created_at
                       ))::float AS duration_sec,
                       NULL::float AS operator_satisfaction,
                       CASE
                           WHEN jsonb_typeof(metadata->'skills_used') = 'array'
                           THEN metadata->'skills_used'
                           ELSE '[]'::jsonb
                       END AS skills_used,
                       0.0 AS similarity
                FROM agent_runs
                WHERE input_message IS NOT NULL
                ORDER BY created_at DESC
                LIMIT 5
        """
        similar_params = {}

    async with UnitOfWork() as uow:
        # 1. Find similar past tasks from persisted agent runs.
        similar_tasks = (await uow.session.execute(
            text(similar_sql),
            similar_params,
        )).mappings().all()

        # 2. Query reconstructive memory nodes for relevant context.
        relevant_memories = (await uow.session.execute(
            text("""
                SELECT id,
                       COALESCE(text, canonical_label) AS content,
                       COALESCE(content_kind, node_kind) AS memory_type,
                       confidence * 10 AS salience,
                       confidence AS similarity
                FROM memory_nodes
                WHERE archived_at IS NULL
                  AND (
                    text ILIKE :pattern OR
                    canonical_label ILIKE :pattern OR
                    normalized_key ILIKE :pattern
                  )
                ORDER BY confidence DESC, updated_at DESC
                LIMIT 10
            """),
            {"pattern": f"%{task_text[:120]}%"},
        )).mappings().all()

        # 3. Find matching skills by embedding similarity (pgvector)
        matching_skills = (await uow.session.execute(
            text("""
                SELECT id, name, maturity, confidence, use_count,
                       success_count::float / GREATEST(use_count, 1) as success_rate,
                       1 - (embedding <=> CAST(:emb AS vector)) as skill_match
                FROM skills
                WHERE NOT archived AND embedding IS NOT NULL
                ORDER BY embedding <=> CAST(:emb AS vector)
                LIMIT 5
            """),
            {"emb": emb_str},
        )).mappings().all()

        # 4. Extract guardrails from lessons/patterns
        guardrails = []
        for m in relevant_memories:
            if m["memory_type"] in ("lesson", "pattern") and m["similarity"] > 0.4:
                guardrails.append(m["content"][:200])

        # 5. Determine strategy based on similar tasks + skill competence
        strategy = "full_pipeline"  # default: safest

        if similar_tasks and similar_tasks[0]["similarity"] > 0.7:
            past = similar_tasks[0]
            if past["outcome"] in ("success", "completed") and past["strategy_chosen"]:
                strategy = past["strategy_chosen"]  # reuse what worked

        if matching_skills:
            top_skill = matching_skills[0]
            if top_skill["maturity"] == "expert" and top_skill["success_rate"] > 0.85:
                strategy = "direct"  # skill is mature enough for direct execution
            elif top_skill["maturity"] in ("emerging", "developing"):
                strategy = "investigate_first"  # not confident enough, investigate first

        # 5b. Classify guardrails by severity and determine blocking status
        blocking_guardrails = []
        advisory_guardrails = []
        for g in guardrails:
            # Guardrails from lessons with high similarity or containing critical keywords are blocking
            g_lower = g.lower()
            is_critical = any(kw in g_lower for kw in [
                "must", "always", "never", "critical", "required", "breaking",
                "data loss", "security", "production", "deploy",
            ])
            if is_critical:
                blocking_guardrails.append(g)
            else:
                advisory_guardrails.append(g)

        blocked = len(blocking_guardrails) > 0

        # 6. Build plan
        plan = {
            "task": task_text,
            "blocked": blocked,
            "strategy": strategy,
            "similar_past_tasks": [{
                "id": t["id"],
                "description": t["description"][:100],
                "outcome": t["outcome"],
                "similarity": round(float(t["similarity"]), 3) if t["similarity"] else 0,
                "strategy_used": t["strategy_chosen"],
            } for t in similar_tasks if t["similarity"] and float(t["similarity"]) > 0.3],
            "recommended_skills": [{
                "name": s["name"],
                "maturity": s["maturity"],
                "confidence": float(s["confidence"]) if s["confidence"] else 0,
                "success_rate": round(float(s["success_rate"]), 3) if s["success_rate"] else 0,
                "match_score": round(float(s["skill_match"]), 3) if s["skill_match"] else 0,
            } for s in matching_skills if s["skill_match"] and float(s["skill_match"]) > 0.3],
            "guardrails": advisory_guardrails[:5],
            "BLOCKING_GUARDRAILS": blocking_guardrails[:5] if blocking_guardrails else [],
            "relevant_memories": [{
                "id": m["id"],
                "content": m["content"][:150],
                "type": m["memory_type"],
                "similarity": round(float(m["similarity"]), 3) if m["similarity"] else 0,
            } for m in relevant_memories[:5]],
        }

        # 7. Store the planning event in agent_runs so future planning can use
        # agent_runs.input_message as the task history.
        row = (await uow.session.execute(text("""
            INSERT INTO agent_runs (
                thread_id, profile, recipe, status, input_message,
                target_ref, workspace_ref, model_policy, metadata,
                completed_at
            ) VALUES (
                'skills-plan', 'cli', 'skill_plan', 'completed', :task,
                '{"kind":"skill_plan"}'::jsonb, '{}'::jsonb, '{}'::jsonb,
                CAST(:metadata AS jsonb), NOW()
            )
            RETURNING id
        """), {
            "task": task_text,
            "metadata": json.dumps({
                "legacy_source": "skills.plan",
                "task_type": "skill_plan",
                "strategy_chosen": strategy,
                "outcome": "planned",
                "similar_past_run_ids": [
                    t["id"] for t in similar_tasks
                    if t["similarity"] and float(t["similarity"]) > 0.3
                ],
                "memory_ids_recalled": [m["id"] for m in relevant_memories[:5]],
                "guardrails": guardrails[:5],
                "skills_used": [
                    s["id"] for s in matching_skills
                    if s["skill_match"] and float(s["skill_match"]) > 0.3
                ],
            }),
        })).mappings().first()
        plan_run_id = int(row["id"])
        await uow.session.execute(text("""
            UPDATE agent_runs
            SET root_run_id = COALESCE(root_run_id, id),
                trace_id = COALESCE(trace_id, 'skills-plan-' || id::text)
            WHERE id = :id
        """), {"id": plan_run_id})
        await uow.session.execute(text("""
            INSERT INTO agent_run_artifacts (
                run_id, root_run_id, artifact_type, title, payload, visibility
            ) VALUES (
                :run_id, :run_id, 'skill_plan', 'Skill planning output',
                CAST(:payload AS jsonb), 'private'
            )
        """), {"run_id": plan_run_id, "payload": json.dumps(plan)})
        plan["plan_run_id"] = plan_run_id

    if blocked:
        plan["blocking_message"] = "These guardrails MUST be addressed before proceeding"

    # Detect skill-authoring tasks and inject mandatory pre-step
    _skill_authoring_keywords = [
        "create skill", "new skill", "write skill", "build skill",
        "modify skill", "update skill", "refine skill", "improve skill",
        "skill authoring", "author skill", "design skill",
    ]
    task_lower = task_text.lower()
    is_skill_authoring = any(kw in task_lower for kw in _skill_authoring_keywords)

    if is_skill_authoring:
        plan["skill_authoring_gate"] = {
            "enforced": True,
            "message": (
                "MANDATORY: This task involves skill authoring. Before proceeding: "
                "1) Review the repository skill-authoring guidance and examples "
                "2) Follow the required structure, naming, and progressive disclosure principles "
                "3) Use --skill-creator-ack flag when running skills.py create/refine"
            ),
        }
        # Promote to blocking if not already
        if not blocked:
            plan["blocked"] = True
            plan["blocking_message"] = "Skill-authoring gate: must read skill-creator before proceeding"

    async with UnitOfWork() as uow:
        await uow.session.execute(text("""
            UPDATE agent_run_artifacts
            SET payload = CAST(:payload AS jsonb)
            WHERE run_id = :run_id AND artifact_type = 'skill_plan'
        """), {"run_id": plan["plan_run_id"], "payload": json.dumps(plan)})

    print(json.dumps(plan, indent=2, default=str))


async def cmd_dashboard(args):
    """Full performance dashboard."""
    async with UnitOfWork() as uow:
        # Skill overview (DB view)
        skills = (await uow.session.execute(text("SELECT * FROM skill_dashboard"))).mappings().all()

        # Recent planning/run outcomes
        task_outcomes = (await uow.session.execute(
            text("""
                SELECT COALESCE(metadata->>'outcome', status) AS outcome, COUNT(*) as cnt
                FROM agent_runs
                WHERE created_at >= CURRENT_DATE - INTERVAL '30 days'
                  AND input_message IS NOT NULL
                GROUP BY outcome
            """)
        )).mappings().all()

        # Skill execution trends (last 7 days)
        exec_trends = (await uow.session.execute(
            text("""
                SELECT s.name, se.outcome, COUNT(*) as cnt
                FROM skill_executions se
                JOIN skills s ON s.id = se.skill_id
                WHERE se.started_at >= CURRENT_DATE - INTERVAL '7 days'
                GROUP BY s.name, se.outcome
            """)
        )).mappings().all()

        # Recent failures for analysis
        recent_failures = (await uow.session.execute(
            text("""
                SELECT se.id, s.name as skill_name, se.task_description,
                       se.error_analysis, se.started_at
                FROM skill_executions se
                JOIN skills s ON s.id = se.skill_id
                WHERE se.outcome = 'failure'
                ORDER BY se.started_at DESC LIMIT 5
            """)
        )).mappings().all()

    result = {
        "skills": [dict(s) for s in skills],
        "task_outcomes_30d": [dict(t) for t in task_outcomes],
        "execution_trends_7d": [dict(e) for e in exec_trends],
        "recent_failures": [dict(f) for f in recent_failures],
    }

    print(json.dumps(result, indent=2, default=str))


async def cmd_evolve(args):
    """Nightly skill evolution — analyze executions and refine skills."""
    print("=" * 60)
    print("SKILL EVOLUTION — Nightly Analysis")
    print("=" * 60)

    async with UnitOfWork() as uow:
        # 1. Analyze recent executions (last 24h)
        recent_execs = (await uow.session.execute(
            text("""
                SELECT se.*, s.name as skill_name, s.pitfalls, s.refinements, s.version
                FROM skill_executions se
                JOIN skills s ON s.id = se.skill_id
                WHERE se.started_at >= CURRENT_DATE - INTERVAL '1 day'
                ORDER BY se.started_at
            """)
        )).mappings().all()

        if not recent_execs:
            print("[evolve] No skill executions in the last 24h")
        else:
            print(f"[evolve] Analyzing {len(recent_execs)} executions")

        for ex in recent_execs:
            skill_id = ex["skill_id"]
            skill = await uow.skills.get(skill_id)
            if not skill:
                continue

            # If failure: analyze and add pitfall
            if ex["outcome"] == "failure" and ex["error_analysis"]:
                pitfalls = list(skill.pitfalls or [])
                already_known = any(ex["error_analysis"][:50] in p.get("text", "") for p in pitfalls)

                if not already_known:
                    pitfalls.append({
                        "text": ex["error_analysis"],
                        "severity": "high",
                        "execution_id": ex["id"],
                        "date": datetime.now().isoformat()
                    })
                    skill.pitfalls = pitfalls
                    skill.updated_at = datetime.now()
                    print(f"  [+pitfall] {ex['skill_name']}: {ex['error_analysis'][:80]}")

            # If refinement proposed: queue it
            if ex["refinement_proposed"]:
                refinements = list(skill.refinements or [])
                refinements.append({
                    "version": skill.version + 1,
                    "change": ex["refinement_proposed"],
                    "reason": f"From execution #{ex['id']}",
                    "date": datetime.now().isoformat(),
                    "auto": True
                })
                skill.refinements = refinements
                skill.version = skill.version + 1
                skill.updated_at = datetime.now()
                print(f"  [+refine] {ex['skill_name']} v{skill.version}: {ex['refinement_proposed'][:80]}")

        # 2. Recompute maturity for all skills
        all_skills = await uow.skills.list_active()

        for s in all_skills:
            use = s.use_count
            rate = s.success_count / max(use, 1)
            maturity, confidence = compute_maturity(use, rate)
            s.maturity = maturity
            s.confidence = confidence

        # 3. Detect potential new skills from recurring agent-run task prompts.
        skill_gaps = (await uow.session.execute(
            text("""
                SELECT input_message AS description,
                       COALESCE(metadata->>'task_type', recipe) AS task_type,
                       COUNT(*) as cnt
                FROM agent_runs
                WHERE input_message IS NOT NULL
                  AND (
                    metadata->'skills_used' IS NULL
                    OR jsonb_typeof(metadata->'skills_used') != 'array'
                    OR jsonb_array_length(metadata->'skills_used') = 0
                  )
                  AND created_at >= CURRENT_DATE - INTERVAL '7 days'
                GROUP BY input_message, COALESCE(metadata->>'task_type', recipe)
                HAVING COUNT(*) >= 2
            """)
        )).mappings().all()

        for gap in skill_gaps:
            print(f"  [!gap] Recurring task without skill: '{gap['description'][:60]}' (x{gap['cnt']})")

        # 4. Detect skills that should be connected (always used together)
        co_occurrences = (await uow.session.execute(
            text("""
                SELECT s1.name as skill1, s2.name as skill2, COUNT(*) as co_occurrence
                FROM skill_executions se1
                JOIN skill_executions se2 ON se2.task_description = se1.task_description AND se2.skill_id > se1.skill_id
                JOIN skills s1 ON s1.id = se1.skill_id
                JOIN skills s2 ON s2.id = se2.skill_id
                GROUP BY s1.name, s2.name
                HAVING COUNT(*) >= 3
            """)
        )).mappings().all()

        for co in co_occurrences:
            print(f"  [~pair] {co['skill1']} + {co['skill2']} used together {co['co_occurrence']}x — consider dependency")

        print(f"\n[evolve] Complete. {len(all_skills)} skills analyzed.")


# ============================================================
# Argument Parser
# ============================================================

async def main():
    parser = argparse.ArgumentParser(description="Illo Skill System")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # list
    subparsers.add_parser("list")

    # get
    p_get = subparsers.add_parser("get")
    p_get.add_argument("name")

    # create
    p_create = subparsers.add_parser("create")
    p_create.add_argument("--name", "-n", required=True)
    p_create.add_argument("--desc", "-d")
    p_create.add_argument("--procedure", "-p", required=True)
    p_create.add_argument("--level", choices=["reflex", "procedural", "cognitive", "meta"])
    p_create.add_argument("--skill-creator-ack", action="store_true",
                          help="Acknowledge that skill-creator SKILL.md was consulted (mandatory gate)")

    # use
    p_use = subparsers.add_parser("use")
    p_use.add_argument("name")
    p_use.add_argument("--task", "-t", required=True)
    p_use.add_argument("--task-type")
    p_use.add_argument("--complexity", type=int)
    p_use.add_argument("--outcome", "-o", choices=["success", "failure", "partial", "abandoned"])
    p_use.add_argument("--duration", type=float)
    p_use.add_argument("--details")

    # refine
    p_refine = subparsers.add_parser("refine")
    p_refine.add_argument("name")
    p_refine.add_argument("--change", required=True)
    p_refine.add_argument("--reason", required=True)
    p_refine.add_argument("--new-procedure")
    p_refine.add_argument("--skill-creator-ack", action="store_true",
                          help="Acknowledge that skill-creator SKILL.md was consulted (mandatory gate)")

    # pitfall
    p_pit = subparsers.add_parser("pitfall")
    p_pit.add_argument("name")
    p_pit.add_argument("--text", required=True)
    p_pit.add_argument("--severity", choices=["low", "medium", "high", "critical"])

    # depend
    p_dep = subparsers.add_parser("depend")
    p_dep.add_argument("--parent", required=True)
    p_dep.add_argument("--child", required=True)
    p_dep.add_argument("--rel", required=True, choices=["requires", "enhances", "optional"])
    p_dep.add_argument("--order", type=int)
    p_dep.add_argument("--strength", type=float)

    # plan
    p_plan = subparsers.add_parser("plan")
    p_plan.add_argument("task")

    # dashboard
    subparsers.add_parser("dashboard")

    # feedback
    p_fb = subparsers.add_parser("feedback")
    p_fb.add_argument("execution_id", type=int)
    p_fb.add_argument("--outcome", choices=["success", "failure", "partial"])
    p_fb.add_argument("--notes")

    # evolve
    subparsers.add_parser("evolve")

    args = parser.parse_args()

    cmd_map = {
        "list": cmd_list,
        "get": cmd_get,
        "create": cmd_create,
        "use": cmd_use,
        "refine": cmd_refine,
        "pitfall": cmd_pitfall,
        "depend": cmd_depend,
        "plan": cmd_plan,
        "dashboard": cmd_dashboard,
        "evolve": cmd_evolve,
    }

    await cmd_map[args.command](args)


if __name__ == "__main__":
    asyncio.run(main())
