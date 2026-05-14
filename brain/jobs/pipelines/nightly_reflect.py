#!/usr/bin/env python3
"""
Illo Brain — LLM-Powered Nightly Reflection

Spawns a provider-neutral LLM call with deep thinking to analyze:
1. Why did skills fail? How should procedures be refined?
2. Are there cross-skill transfer opportunities?
3. What new skills should emerge from recurring patterns?
4. What system improvements should be proposed?

Uses the configured runtime provider via brain.systems.runs.direct_agent.call_llm.
Outputs: skill refinements, new memories, journal entry, system proposals.

Run: python3 nightly_reflect.py [--date 2026-03-02]
"""

import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, date

from sqlalchemy import text

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))  # repo root
import brain.kernel.config as config
from brain.platform.db.repositories.unit_of_work import UnitOfWork

WORKSPACE = str(config.WORKSPACE_ROOT)
PRIVATE_HOME = str(config.PRIVATE_HOME)

async def gather_context(target_date: date, org_id: str | None = None) -> dict:
    """Gather all data the LLM needs for reflection."""
    # Only memories has org_id. Other tables are org-wide for now.
    _mem_org_filter = "AND org_id = :org_id" if org_id else ""
    _mem_org_params = {"org_id": org_id} if org_id else {}

    async with UnitOfWork() as uow:
        context = {}

        # 1. Today's skill executions
        result = await uow.session.execute(text("""
            SELECT se.*, s.name as skill_name, s.procedure, s.pitfalls, s.version
            FROM skill_executions se
            JOIN skills s ON s.id = se.skill_id
            WHERE se.started_at::date = :target_date
            ORDER BY se.started_at
        """), {"target_date": target_date})
        context["skill_executions"] = [dict(r) for r in result.mappings().all()]

        # 2. All skills with current state
        result = await uow.session.execute(text("""
            SELECT id, name, description, procedure, version, maturity, confidence,
                   use_count, success_count, failure_count, pitfalls, refinements
            FROM skills WHERE NOT archived
        """))
        context["skills"] = [dict(r) for r in result.mappings().all()]

        # 5. Today's retrieval log
        result = await uow.session.execute(text("""
            SELECT query_text, results_returned, top_score, was_relevant, feedback
            FROM retrieval_log
            WHERE timestamp::date = :target_date
        """), {"target_date": target_date})
        context["retrievals"] = [dict(r) for r in result.mappings().all()]

        # 5b. Consistently-missed memories (retrieval feedback loop)
        try:
            from brain.systems.memory.retrieval_feedback import analyze_missed_memories
            context["consistently_missed_memories"] = await analyze_missed_memories(min_misses=3, days=30)
        except Exception as e:
            print(f"[reflect] Warning: retrieval feedback analysis failed: {e}")
            context["consistently_missed_memories"] = []

        # 6. Today's task-like prompts, now sourced from agent_runs.input_message.
        result = await uow.session.execute(text("""
            SELECT input_message AS description,
                   COALESCE(metadata->>'task_type', recipe) AS task_type,
                   COALESCE(metadata->>'strategy_chosen', recipe) AS strategy_chosen,
                   COALESCE(metadata->>'outcome', status) AS outcome,
                   EXTRACT(EPOCH FROM (
                       COALESCE(completed_at, failed_at, canceled_at, updated_at, created_at) - created_at
                   ))::float AS duration_sec,
                   NULL::float AS operator_satisfaction,
                   CASE
                       WHEN jsonb_typeof(metadata->'guardrails') = 'array'
                       THEN metadata->'guardrails'
                       WHEN jsonb_typeof(metadata->'guardrails_injected') = 'array'
                       THEN metadata->'guardrails_injected'
                       ELSE '[]'::jsonb
                   END AS guardrails,
                   COALESCE(metadata->>'feedback_notes', metadata->>'outcome_notes') AS feedback_notes
            FROM agent_runs
            WHERE created_at::date = :target_date
              AND input_message IS NOT NULL
            ORDER BY created_at
        """), {"target_date": target_date})
        context["tasks"] = [dict(r) for r in result.mappings().all()]

        # 5. New memories created today (scoped by org_id if provided)
        params = {"target_date": target_date, **_mem_org_params}
        result = await uow.session.execute(text(f"""
            SELECT id, content, memory_type, salience, source
            FROM memories
            WHERE created_at::date = :target_date AND NOT archived {_mem_org_filter}
            ORDER BY salience DESC
        """), params)
        context["new_memories"] = [dict(r) for r in result.mappings().all()]

        # 8. Previous daily metrics (for comparison)
        result = await uow.session.execute(text("""
            SELECT * FROM daily_metrics
            WHERE metric_date >= :target_date - INTERVAL '7 days'
            ORDER BY metric_date DESC
        """), {"target_date": target_date})
        context["previous_metrics"] = [dict(r) for r in result.mappings().all()]

        # 9. Agent run activity.
        try:
            result = await uow.session.execute(text("""
                SELECT id, trace_id, thread_id, parent_run_id, root_run_id,
                       profile, recipe, status,
                       COALESCE(metadata->>'skill_used', metadata->>'skill_name') AS skill_used,
                       model_policy->>'model' AS model_used,
                       created_at, started_at, completed_at, failed_at, canceled_at,
                       target_ref, workspace_ref, model_policy,
                       metadata->'error_classification' AS error_classification,
                       metadata->'postmortem' AS postmortem,
                       LEFT(input_message, 500) as message_preview,
                       LEFT(COALESCE(metadata->>'error', metadata->>'outcome_notes'), 2000) as error
                FROM agent_runs
                WHERE created_at::date = :target_date
                ORDER BY created_at
            """), {"target_date": target_date})
            context["agent_runses"] = [dict(r) for r in result.mappings().all()]
        except Exception as e:
            print(f"[reflect] Warning: agent_runs query failed: {e}")
            context["agent_runses"] = []

        # 10. Today's daily log if exists
        daily_file = os.path.join(str(config.JOURNAL_DIR), f"{target_date.isoformat()}.md")
        if os.path.exists(daily_file):
            with open(daily_file) as f:
                context["daily_log"] = f.read()[:5000]

    return context


def _format_failures_by_category(runs: list) -> str:
    """Group failed runs by error classification category."""
    failures = defaultdict(list)
    for d in runs:
        if d.get("status") not in ("failed", "timeout"):
            continue
        cls = d.get("error_classification") or {}
        cat = cls.get("category", "unclassified")
        summary = cls.get("summary", (d.get("error") or "unknown")[:120])
        skill = d.get("skill_used") or "none"
        tokens = d.get("tokens_total") or 0
        failures[cat].append(f"- Run #{d['id']}: {summary} (skill: {skill}, tokens: {tokens:,})")
    if not failures:
        return "No failures today."
    parts = []
    for cat, items in sorted(failures.items()):
        parts.append(f"#### {cat} ({len(items)} run{'es' if len(items) != 1 else ''})")
        parts.extend(items)
    return "\n".join(parts)


def build_reflection_prompt(context: dict, target_date: date) -> str:
    """Build the prompt for the LLM reflection agent."""

    prompt = f"""You are the self-reflection module of Illo's brain — an AI agent's metacognitive system.

Today is {target_date.isoformat()}. You are analyzing today's data to improve the system.

## Your Data

### Skill Executions (today)
{json.dumps(context['skill_executions'], indent=2, default=str)}

### Current Skills
{json.dumps(context['skills'], indent=2, default=str)}

### Retrieval Log (today)
{json.dumps(context['retrievals'], indent=2, default=str)}

### Consistently Missed Memories (30d, ≥3 misses)
{json.dumps(context.get('consistently_missed_memories', []), indent=2, default=str)}

### Tasks (today)
{json.dumps(context['tasks'], indent=2, default=str)}

### Cortex Runs (today)
{json.dumps(context['agent_runses'], indent=2, default=str)}

### Cortex Failures by Category
{_format_failures_by_category(context['agent_runses'])}

### New Memories (today)
{json.dumps(context['new_memories'], indent=2, default=str)}

### Previous Daily Metrics
{json.dumps(context['previous_metrics'], indent=2, default=str)}

### Daily Log
{context.get('daily_log', 'No daily log found.')}

## Your Analysis Tasks

Think deeply about each of these. Use extended reasoning.

### 1. Skill Performance Analysis
- For each skill execution today: did it succeed? Why or why not?
- For failures: what was the root cause? What step in the procedure failed?
- What specific changes to skill procedures would prevent these failures?
- Are any skills mature enough to level up? Are any declining?

### 2. Retrieval Quality Analysis
- Were the right memories surfaced when needed?
- Any cases where important context was missed?
- Should retrieval weights be adjusted? (Current: semantic, salience, recency, and frequency signals)

### 3. Skill Emergence Detection
- Were there any tasks or actions today that don't fit existing skills?
- Should a new skill be created? If so, define its initial procedure.
- Should any existing skills be split, merged, or have dependencies added?

### 4. Cross-Skill Insights
- Did learning in one area transfer to another?
- Are there abstract patterns that apply across multiple skills?

### 5. Run Telemetry Analysis
- Review the Cortex Failures by Category section above.
- For each failure category: is this a systemic issue or a one-off?
- For context_overflow: what caused the context to grow? Should session trimming be more aggressive?
- For tool_failure: which tools are failing? Should skill procedures be updated?
- For stuck_loop: what patterns are being repeated? What guardrails would prevent this?
- Review post-mortem lessons (in the run data) — are they actionable?
- Are any skills consistently failing? What's the failure-to-success ratio per skill?

### 6. System Improvement Proposals
- What should change about the memory system, skill system, or nightly process?
- Are there tools or capabilities Illo should ask the operator for?
- What experiments should we try?

## Output Format

Respond with a JSON object (and nothing else) with this structure:
```json
{{
    "date": "{target_date.isoformat()}",
    "skill_refinements": [
        {{
            "skill_name": "...",
            "change_type": "refine_procedure|add_pitfall|add_step|remove_step|update_description",
            "change": "...",
            "reason": "...",
            "new_procedure": "..."
        }}
    ],
    "new_skills_proposed": [
        {{
            "name": "...",
            "description": "...",
            "initial_procedure": "...",
            "emerged_from": "..."
        }}
    ],
    "retrieval_adjustments": {{
        "quality_assessment": "good|needs_work|poor",
        "weight_changes": {{}},
        "notes": "..."
    }},
    "system_proposals": [
        {{
            "area": "memory|skills|retrieval|consolidation|other",
            "proposal": "...",
            "priority": "high|medium|low",
            "reason": "..."
        }}
    ],
    "journal_entry": "A 2-3 paragraph narrative summary of today's analysis for the evolution journal. Include specific metrics, what improved, what declined, and what actions were taken.",
    "daily_metrics_update": {{
        "competence_architecture": 0.0,
        "competence_debugging": 0.0,
        "competence_frontend": 0.0,
        "competence_provider_apis": 0.0,
        "competence_communication": 0.0,
        "competence_proactivity": 0.0,
        "reflection_notes": "...",
        "behavioral_adjustments": "..."
    }}
}}
```
"""
    return prompt


async def apply_reflection(reflection: dict, target_date: date, context: dict | None = None):
    """Apply the LLM's reflection outputs to the system."""
    context = context or {}

    async with UnitOfWork() as uow:
        applied = []

        # 1. Apply skill refinements
        for ref in reflection.get("skill_refinements", []):
            skill_name = ref.get("skill_name")
            if not skill_name:
                continue

            result = await uow.session.execute(text(
                "SELECT id, version, pitfalls, refinements FROM skills WHERE name = :name"
            ), {"name": skill_name})
            skill = result.mappings().first()
            if not skill:
                continue

            skill_id = skill["id"]
            version = skill["version"]
            pitfalls = skill["pitfalls"]
            refinements = skill["refinements"]

            if ref["change_type"] == "add_pitfall":
                pitfalls = pitfalls or []
                pitfalls.append({
                    "text": ref["change"],
                    "severity": "high",
                    "source": "nightly_reflection",
                    "date": target_date.isoformat()
                })
                await uow.session.execute(text(
                    "UPDATE skills SET pitfalls = :pitfalls, updated_at = NOW() WHERE id = :id"
                ), {"pitfalls": json.dumps(pitfalls), "id": skill_id})
                applied.append(f"Added pitfall to {skill_name}: {ref['change'][:60]}")

            elif ref["change_type"] in ("refine_procedure", "add_step", "remove_step"):
                new_version = version + 1
                refinements = refinements or []
                refinements.append({
                    "version": new_version,
                    "change": ref["change"],
                    "reason": ref.get("reason", "nightly reflection"),
                    "date": target_date.isoformat(),
                    "auto": True
                })

                new_proc = ref.get("new_procedure")
                if new_proc:
                    await uow.session.execute(text("""
                        UPDATE skills SET version = :version, procedure = :procedure,
                               refinements = :refinements, updated_at = NOW()
                        WHERE id = :id
                    """), {"version": new_version, "procedure": new_proc,
                           "refinements": json.dumps(refinements), "id": skill_id})
                else:
                    await uow.session.execute(text("""
                        UPDATE skills SET version = :version, refinements = :refinements,
                               updated_at = NOW()
                        WHERE id = :id
                    """), {"version": new_version, "refinements": json.dumps(refinements),
                           "id": skill_id})

                applied.append(f"Refined {skill_name} to v{new_version}: {ref['change'][:60]}")

        # 2. Create proposed new skills (with centralized gate — issue #187)
        from brain.systems.skills.gate import validate_skill_structure
        for prop in reflection.get("new_skills_proposed", []):
            if not prop.get("name") or not prop.get("initial_procedure"):
                continue

            result = await uow.session.execute(text(
                "SELECT id FROM skills WHERE name = :name"
            ), {"name": prop["name"]})
            if result.mappings().first():
                continue  # already exists

            # Validate via skill-creator gate before insertion
            violations = validate_skill_structure(
                prop["name"], prop.get("description", ""), prop["initial_procedure"],
            )
            if violations:
                applied.append(
                    f"Skill '{prop['name']}' blocked by gate: {'; '.join(violations)}"
                )
                continue

            await uow.session.execute(text("""
                INSERT INTO skills (name, description, procedure, auto_emerged)
                VALUES (:name, :description, :procedure, TRUE)
            """), {"name": prop["name"], "description": prop.get("description", ""),
                   "procedure": prop["initial_procedure"]})
            applied.append(f"New skill emerged: {prop['name']}")

        # 3. Update daily metrics
        metrics = reflection.get("daily_metrics_update", {})
        if metrics:
            await uow.session.execute(text("""
                INSERT INTO daily_metrics (metric_date,
                    competence_architecture, competence_debugging, competence_frontend,
                    competence_provider_apis, competence_communication, competence_proactivity,
                    reflection_notes, behavioral_adjustments,
                    agent_runses, skill_executions_count)
                VALUES (:metric_date, :competence_architecture, :competence_debugging,
                    :competence_frontend, :competence_provider_apis, :competence_communication,
                    :competence_proactivity, :reflection_notes, :behavioral_adjustments,
                    :agent_runses, :skill_executions_count)
                ON CONFLICT (metric_date) DO UPDATE SET
                    competence_architecture = EXCLUDED.competence_architecture,
                    competence_debugging = EXCLUDED.competence_debugging,
                    competence_frontend = EXCLUDED.competence_frontend,
                    competence_provider_apis = EXCLUDED.competence_provider_apis,
                    competence_communication = EXCLUDED.competence_communication,
                    competence_proactivity = EXCLUDED.competence_proactivity,
                    reflection_notes = EXCLUDED.reflection_notes,
                    behavioral_adjustments = EXCLUDED.behavioral_adjustments,
                    agent_runses = EXCLUDED.agent_runses,
                    skill_executions_count = EXCLUDED.skill_executions_count
            """), {
                "metric_date": target_date,
                "competence_architecture": metrics.get("competence_architecture"),
                "competence_debugging": metrics.get("competence_debugging"),
                "competence_frontend": metrics.get("competence_frontend"),
                "competence_provider_apis": metrics.get("competence_provider_apis"),
                "competence_communication": metrics.get("competence_communication"),
                "competence_proactivity": metrics.get("competence_proactivity"),
                "reflection_notes": metrics.get("reflection_notes"),
                "behavioral_adjustments": metrics.get("behavioral_adjustments"),
                "agent_runses": len(context.get("agent_runses", [])),
                "skill_executions_count": len(context.get("skill_executions", [])),
            })
            applied.append("Updated daily metrics")

    # 4. Write journal entry (outside UoW — file I/O only)
    journal_text = reflection.get("journal_entry", "")
    if journal_text:
        journal_dir = str(config.JOURNAL_DIR)
        os.makedirs(journal_dir, exist_ok=True)

        # Use YYYY-MM-DD.md naming (no numeric prefix)
        journal_path = os.path.join(journal_dir, f"{target_date.isoformat()}.md")

        with open(journal_path, 'w') as f:
            f.write(f"# Nightly Reflection — {target_date.isoformat()}\n\n")
            f.write(f"*Auto-generated by LLM reflection at {datetime.now().isoformat()}*\n\n")
            f.write(journal_text)
            f.write("\n\n## Actions Taken\n")
            for a in applied:
                f.write(f"- {a}\n")

            # Add system proposals
            proposals = reflection.get("system_proposals", [])
            if proposals:
                f.write("\n## System Improvement Proposals\n")
                for p in proposals:
                    f.write(f"- [{p.get('priority', 'medium').upper()}] {p.get('area', '?')}: {p.get('proposal', '')}\n")

        applied.append(f"Journal entry written: {journal_path}")

    return applied


async def run_reflection(target_date: date):
    """Main reflection flow: gather data → prompt LLM → apply results."""
    print(f"{'='*60}")
    print(f"LLM NIGHTLY REFLECTION — {target_date}")
    print(f"{'='*60}")

    # 1. Gather context
    print("[reflect] Gathering context...")
    context = await gather_context(target_date)

    # Check if there's anything to reflect on
    total_data = (len(context["skill_executions"]) +
                  len(context["tasks"]) + len(context["new_memories"]) +
                  len(context["agent_runses"]))

    if total_data == 0:
        print("[reflect] No data to reflect on today. Skipping.")
        return

    print(f"[reflect] Data: {len(context['skill_executions'])} skill execs, "
          f"{len(context['tasks'])} tasks, "
          f"{len(context['agent_runses'])} cortex runs, "
          f"{len(context['new_memories'])} new memories")

    # 2. Build prompt
    prompt = build_reflection_prompt(context, target_date)

    # 3. Write prompt to temp file for the LLM
    prompt_path = os.path.join(str(config.BRAIN_LOG_DIR), f"reflect-prompt-{target_date}.md")
    os.makedirs(str(config.BRAIN_LOG_DIR), exist_ok=True)
    with open(prompt_path, 'w') as f:
        f.write(prompt)

    # 4. Call the configured provider for deep analysis
    print("[reflect] Calling configured LLM for deep analysis...")

    output_path = os.path.join(str(config.BRAIN_LOG_DIR), f"reflect-output-{target_date}.json")
    reflection = None

    try:
        from brain.systems.runs.direct_agent import call_llm
        reflection = call_llm(prompt, thinking="high")
    except Exception as e:
        print(f"[reflect] API call failed: {e}")

    # Fallback: save for main agent to process
    if reflection is None:
        print("[reflect] Direct CLI failed. Saving prompt for main agent processing.")
        pending_path = os.path.join(str(config.BRAIN_LOG_DIR), f"reflect-pending-{target_date}.md")
        with open(pending_path, 'w') as f:
            f.write(prompt)

        # Also save a flag file for the main agent's wake-up
        flag_path = os.path.join(PRIVATE_HOME, "PENDING_REFLECTION.json")
        with open(flag_path, 'w') as f:
            json.dump({
                "date": target_date.isoformat(),
                "prompt_path": pending_path,
                "output_path": output_path,
                "created": datetime.now().isoformat()
            }, f)

        print(f"[reflect] Flag file written: {flag_path}")
        print("[reflect] Main agent will process this on next wake-up.")
        return

    # 5.5 Meta-skill analysis
    try:
        from meta_skills import nightly_meta_analysis
        print("[reflect] Running meta-skill analysis...")
        meta_summary = await nightly_meta_analysis()
        print(meta_summary)
    except Exception as e:
        print(f"[reflect] Meta-skill analysis failed: {e}")
        meta_summary = None

    # 6. Save full reflection for review.
    with open(output_path, 'w') as f:
        json.dump(reflection, f, indent=2, default=str)

    print("\n[reflect] Complete.")
    print(f"[reflect] Full reflection saved to {output_path}")

    # Memory quality sweep
    try:
        from quality_gate import sweep_low_quality
        flagged = sweep_low_quality(dry_run=True)
        if flagged:
            print(f"\n[quality] Flagged {len(flagged)} low-quality memories:")
            for f in flagged:
                print(f"  #{f['id']}: {f['reason']} — {f.get('content', '')[:60]}")
            reflection["quality_sweep"] = flagged
        else:
            print("\n[quality] No low-quality memories found.")
            reflection["quality_sweep"] = []
    except Exception as e:
        print(f"\n[quality] Sweep failed: {e}")
        reflection["quality_sweep_error"] = str(e)


def main():
    parser = argparse.ArgumentParser(description="Illo LLM Nightly Reflection")
    parser.add_argument("--date", help="Target date (YYYY-MM-DD), default today")
    args = parser.parse_args()

    target = date.fromisoformat(args.date) if args.date else date.today()
    asyncio.run(run_reflection(target))


if __name__ == "__main__":
    main()
