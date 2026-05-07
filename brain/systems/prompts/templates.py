#!/usr/bin/env python3
"""Prompt template storage — versioned templates that evolve from experience.

Stores prompt templates in DB with version tracking, quality scoring,
and outcome history. Templates improve over time based on delegation results.

Closes #74 (Self-Improving Prompts).
"""

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))

from sqlalchemy import func, select, text

from brain.platform.db.models.prompt import PromptTemplate, PromptTemplateOutcome
from brain.platform.db.repositories.unit_of_work import UnitOfWork

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def ensure_tables():
    """Create prompt_templates and prompt_template_outcomes tables."""
    with UnitOfWork() as uow:
        uow.session.execute(text("""
            CREATE TABLE IF NOT EXISTS prompt_templates (
                id SERIAL PRIMARY KEY,
                name TEXT NOT NULL,
                template_text TEXT NOT NULL,
                version INT NOT NULL DEFAULT 1,
                avg_quality_score FLOAT DEFAULT 0.0,
                use_count INT DEFAULT 0,
                last_used TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(name, version)
            )
        """))
        uow.session.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_prompt_templates_name
            ON prompt_templates (name)
        """))
        uow.session.execute(text("""
            CREATE TABLE IF NOT EXISTS prompt_template_outcomes (
                id SERIAL PRIMARY KEY,
                template_name TEXT NOT NULL,
                template_version INT NOT NULL,
                quality_score FLOAT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """))
        uow.session.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_prompt_template_outcomes_name
            ON prompt_template_outcomes (template_name, template_version)
        """))


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def get_template(name: str) -> dict | None:
    """Get the latest version of a template by name."""
    ensure_tables()
    with UnitOfWork() as uow:
        stmt = (
            select(PromptTemplate)
            .where(PromptTemplate.name == name)
            .order_by(PromptTemplate.version.desc())
            .limit(1)
        )
        row = uow.session.scalars(stmt).first()
        if row is None:
            return None
        return {
            "id": row.id,
            "name": row.name,
            "template_text": row.template_text,
            "version": row.version,
            "avg_quality_score": row.avg_quality_score,
            "use_count": row.use_count,
            "last_used": row.last_used,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }


def get_template_version(name: str, version: int) -> dict | None:
    """Get a specific version of a template."""
    ensure_tables()
    with UnitOfWork() as uow:
        stmt = select(PromptTemplate).where(
            PromptTemplate.name == name,
            PromptTemplate.version == version,
        )
        row = uow.session.scalars(stmt).first()
        if row is None:
            return None
        return {
            "id": row.id,
            "name": row.name,
            "template_text": row.template_text,
            "version": row.version,
            "avg_quality_score": row.avg_quality_score,
            "use_count": row.use_count,
            "last_used": row.last_used,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }


def list_templates() -> list[dict]:
    """List all templates (latest version of each)."""
    ensure_tables()
    with UnitOfWork() as uow:
        # DISTINCT ON is PG-specific, use text() for it
        rows = uow.session.execute(text("""
            SELECT DISTINCT ON (name) *
            FROM prompt_templates
            ORDER BY name, version DESC
        """)).mappings().all()
        return [dict(r) for r in rows]


def create_template(name: str, template_text: str, version: int = 1) -> int:
    """Create a new template (or new version). Returns the id."""
    ensure_tables()
    with UnitOfWork() as uow:
        tmpl = PromptTemplate(
            name=name,
            template_text=template_text,
            version=version,
        )
        uow.session.add(tmpl)
        uow.session.flush()
        return tmpl.id


def record_use(name: str):
    """Increment use_count and update last_used for the latest version."""
    ensure_tables()
    with UnitOfWork() as uow:
        # Subquery to find the latest version's id
        uow.session.execute(text("""
            UPDATE prompt_templates
            SET use_count = use_count + 1, last_used = NOW(), updated_at = NOW()
            WHERE id = (
                SELECT id FROM prompt_templates
                WHERE name = :name ORDER BY version DESC LIMIT 1
            )
        """), {"name": name})


# ---------------------------------------------------------------------------
# Outcome tracking
# ---------------------------------------------------------------------------

def record_template_outcome(template_name: str, version: int, quality_score: float) -> int:
    """Record an outcome for a template and update its avg score."""
    ensure_tables()
    with UnitOfWork() as uow:
        outcome = PromptTemplateOutcome(
            template_name=template_name,
            template_version=version,
            quality_score=quality_score,
        )
        uow.session.add(outcome)
        uow.session.flush()
        outcome_id = outcome.id

        # Recompute average
        avg_stmt = select(func.avg(PromptTemplateOutcome.quality_score)).where(
            PromptTemplateOutcome.template_name == template_name,
            PromptTemplateOutcome.template_version == version,
        )
        avg = uow.session.scalar(avg_stmt) or 0.0

        # Update the template's avg score
        tmpl_stmt = select(PromptTemplate).where(
            PromptTemplate.name == template_name,
            PromptTemplate.version == version,
        )
        tmpl = uow.session.scalars(tmpl_stmt).first()
        if tmpl:
            tmpl.avg_quality_score = avg
            tmpl.updated_at = datetime.now(timezone.utc)
            uow.session.flush()

        return outcome_id


def get_outcome_history(template_name: str, limit: int = 20) -> list[dict]:
    """Get recent outcomes for a template."""
    ensure_tables()
    with UnitOfWork() as uow:
        stmt = (
            select(PromptTemplateOutcome)
            .where(PromptTemplateOutcome.template_name == template_name)
            .order_by(PromptTemplateOutcome.created_at.desc())
            .limit(limit)
        )
        rows = uow.session.scalars(stmt).all()
        return [
            {
                "id": r.id,
                "template_name": r.template_name,
                "template_version": r.template_version,
                "quality_score": r.quality_score,
                "created_at": r.created_at,
            }
            for r in rows
        ]


def get_underperforming_templates(threshold: float = 0.6) -> list[dict]:
    """Get templates with avg_quality_score below threshold and at least 3 uses."""
    ensure_tables()
    with UnitOfWork() as uow:
        # DISTINCT ON is PG-specific
        rows = uow.session.execute(text("""
            SELECT DISTINCT ON (name) *
            FROM prompt_templates
            WHERE avg_quality_score < :threshold AND use_count >= 3
            ORDER BY name, version DESC
        """), {"threshold": threshold}).mappings().all()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Seed templates
# ---------------------------------------------------------------------------

SEED_TEMPLATES = {
    "code_review": """\
## Code Review Task

Review the following code changes for: {task}

### Checklist
- [ ] Logic correctness — does it do what it claims?
- [ ] DRY — no unnecessary duplication
- [ ] Error handling — edge cases covered
- [ ] Tests — adequate coverage for changes
- [ ] Naming — clear, consistent variable/function names
- [ ] Security — no credentials, injection risks, or unsafe patterns

### Requirements
- Provide specific line-level feedback
- Suggest concrete improvements, not vague advice
- Flag any missing tests
- Rate overall quality (A+ to F)

{guardrails}
""",
    "bug_fix": """\
## Bug Fix Task

Fix the following issue: {task}

### Approach
1. **Reproduce** — verify the bug exists with actual values, not assumptions
2. **Trace end-to-end** — follow the data flow from input to output
3. **Identify root cause** — don't patch symptoms
4. **Fix** — minimal, targeted change
5. **Verify** — confirm the fix resolves the original issue
6. **Regression check** — ensure nothing else broke

### Guardrails
- Verify actual values at each step — no speculation
- Show evidence (logs, test output) for your diagnosis
- If the bug is not where you expect, widen the search

{guardrails}
""",
    "investigation": """\
## Investigation Task

Investigate: {task}

### Methodology
1. **Define the question** clearly before starting
2. **Gather data** — query, don't assume. Show actual values.
3. **Form hypotheses** — list possibilities ranked by likelihood
4. **Test hypotheses** — with real data, not speculation
5. **Report findings** — data-backed conclusions only

### Guardrails
- Show data, not speculation
- Query don't assume — run the actual commands
- Distinguish between "confirmed" and "suspected"
- If data is insufficient, say so — don't fill gaps with guesses

{guardrails}
""",
    "quality_improvement": """\
## Quality Improvement Task

Improve quality of: {task}

### Framework
1. **Define A+ standard** for each quality dimension
2. **Assess current state** against A+ standard
3. **Identify gaps** — prioritize by impact
4. **Implement improvements** — one dimension at a time
5. **Verify** — measurable improvement demonstrated

### Guardrails
- Define what "good" looks like before starting
- Measure before and after
- Don't gold-plate — focus on highest-impact gaps

{guardrails}
""",
    "feature_build": """\
## Feature Build Task

Build: {task}

### Workflow
1. **Clarify requirements** — restate what's being built and acceptance criteria
2. **Design** — outline approach before coding
3. **TDD** — write tests first for core logic
4. **Implement** — clean, minimal code that passes tests
5. **Integration** — wire into existing system
6. **Verify** — all tests pass, manual smoke test
7. **PR** — clean commits, descriptive PR description

### Guardrails
- Write tests before implementation
- One concern per commit
- Verify end-to-end before marking complete
- If scope creeps, flag it — don't silently expand

{guardrails}
""",
}


def seed_templates():
    """Insert seed templates if they don't exist yet."""
    ensure_tables()
    for name, text in SEED_TEMPLATES.items():
        existing = get_template(name)
        if not existing:
            create_template(name, text)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Prompt template management")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("seed", help="Seed initial templates")
    sub.add_parser("list", help="List all templates")

    p_get = sub.add_parser("get", help="Get a template")
    p_get.add_argument("name")

    args = parser.parse_args()

    if args.command == "seed":
        seed_templates()
        print("Seeded templates.")
    elif args.command == "list":
        for t in list_templates():
            print(f"  {t['name']} v{t['version']}  score={t['avg_quality_score']:.2f}  uses={t['use_count']}")
    elif args.command == "get":
        t = get_template(args.name)
        if t:
            print(f"# {t['name']} v{t['version']}\n")
            print(t["template_text"])
        else:
            print(f"Template '{args.name}' not found")
