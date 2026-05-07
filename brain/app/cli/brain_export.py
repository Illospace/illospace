#!/usr/bin/env python3
"""Export universal memories and skills as a shareable starter kit.

Usage:
    python3 -m brain.app.cli.brain_export --scope universal --output ./export/
    python3 -m brain.app.cli.brain_export --scope universal --output ./export/ --dry-run
    python3 -m brain.app.cli.brain_export --scope universal --output ./export/ --skip-llm
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), *([".."] * 3))))

from sqlalchemy import text

import brain.kernel.config as config
from brain.platform.db.repositories.unit_of_work import UnitOfWork

WORKSPACE = str(config.WORKSPACE_ROOT)


# ============================================================
# Layer 1: Regex scrubbing
# ============================================================

SCRUB_PATTERNS = [
    # IP addresses
    (r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b', '<IP_ADDRESS>'),
    # Tokens / secrets
    (r'\b(?:ghp_|github_pat_|sk-|xox[bpas]-)\S+', '<TOKEN>'),
    (r'Bearer\s+\S+', 'Bearer <TOKEN>'),
    # Email addresses
    (r'\b[\w.+-]+@[\w-]+\.[\w.]+\b', '<EMAIL>'),
    # Specific file paths
    (r'/home/\w+/[^\s]+', '<FILE_PATH>'),
    (r'/var/[^\s]+', '<FILE_PATH>'),
    # URLs with specific domains (keep generic doc URLs)
    (r'https?://(?!(?:docs\.|en\.wikipedia|stackoverflow|developer\.))[a-zA-Z0-9.-]+\.[a-z]{2,}/[^\s]*', '<URL>'),
    # Contextual personal names
    (r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?(?=\s+(?:said|asked|prefers|learned|reported|found|corrected|at)\b)", 'the user'),
    (r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?(?='s\s+(?:contact|email|preference|preferences)\b)", 'the user'),
    # Known company/product names
    (r'\b[Uu]wear(?:\.ai)?\b', 'the product'),
    (r'\b(?:illo-backend|illoaiapp|shopify-app-2025)\b', 'the repo'),
    (r'\billo(?:-brain)?\b', 'the agent'),
]


def regex_scrub(text: str) -> str:
    """Layer 1: Deterministic regex scrubbing."""
    result = text
    for pattern, replacement in SCRUB_PATTERNS:
        result = re.sub(pattern, replacement, result)
    return result


# ============================================================
# Layer 2: LLM scrubbing
# ============================================================

LLM_SCRUB_PROMPT = (
    "Rewrite this memory to be universal. Remove all identifying information "
    "about specific people, companies, projects, and locations. Preserve the "
    "core lesson/principle/technique. If the memory is already universal, "
    "return it unchanged. Return ONLY the rewritten text, nothing else."
)


def llm_scrub(text: str) -> str:
    """Layer 2: LLM-based contextual scrubbing via the configured provider."""
    full_prompt = f"{LLM_SCRUB_PROMPT}\n\nMemory:\n{text}"
    try:
        from brain.systems.runs.invocation import build_direct_agent_invocation, invoke_direct_agent
        spec = build_direct_agent_invocation(
            message=full_prompt,
            thinking="none",
            tools=[],
            persist_session=False,
            max_turns=1,
            tool_call_source="brain_export",
        )
        result = invoke_direct_agent(spec)
        if result.success and result.output.strip():
            return result.output.strip()
    except Exception:
        pass
    # Fallback: return regex-only version
    return text


def scrub_content(text: str, skip_llm: bool = False) -> str:
    """3-layer scrubbing: regex → LLM → human spot-check (noted in README)."""
    # Layer 1: regex
    scrubbed = regex_scrub(text)
    # Layer 2: LLM (unless skipped)
    if not skip_llm:
        scrubbed = llm_scrub(scrubbed)
    return scrubbed


# ============================================================
# Export functions
# ============================================================

def export_memories(scope: str, skip_llm: bool = False) -> list[dict]:
    """Fetch and scrub memories of the given scope."""
    with UnitOfWork() as uow:
        rows = [dict(r) for r in uow.session.execute(text("""
            SELECT id, content, memory_type, salience, emotion_label,
                   tags, source, created_at, scope
            FROM memories
            WHERE scope = :scope AND NOT archived
            ORDER BY salience DESC, created_at DESC
        """), {"scope": scope}).mappings().all()]

    exported = []
    for i, row in enumerate(rows):
        print(f"  Scrubbing memory {i+1}/{len(rows)} (id={row['id']})...")
        scrubbed = scrub_content(row['content'], skip_llm=skip_llm)
        exported.append({
            'content': scrubbed,
            'memory_type': row['memory_type'],
            'salience': row['salience'],
            'emotion_label': row['emotion_label'],
            'tags': row['tags'] or [],
            'source': 'starter_kit',
            'original_scope': row['scope'],
        })
    return exported


def export_skills() -> list[dict]:
    """Export all non-archived skills."""
    with UnitOfWork() as uow:
        rows = uow.session.execute(text("""
            SELECT name, description, procedure, version, level, maturity,
                   confidence, use_count, success_count, failure_count,
                   partial_count, avg_duration_sec, pitfalls, refinements,
                   triggers
            FROM skills WHERE NOT archived
            ORDER BY use_count DESC
        """)).mappings().all()
        return [dict(r) for r in rows]


def generate_operator_context_readme() -> str:
    """Generate documentation for private operator context files."""
    return """# Operator Context Templates

Illo Brain stores personalized agent/operator context outside source control by
default. Put local prompts, identity notes, heartbeat reminders, and generated
checklists under `ILLO_PRIVATE_HOME` (defaults to `.illo/`) or set
`AGENT_CONTEXT_DIR` to another private path.

Suggested local files:

- `agent-context/operator.md` — local operating principles and identity.
- `agent-context/user.md` — private user/workspace preferences.
- `agent-context/heartbeat.md` — local recurring reminders.
- `agent-context/pre-flight-checklist.md` — generated guardian checklist.

These files are intentionally not required for the application to boot and are
not copied into public starter-kit exports. Runtime memory, skills, vault
secrets, projects, and threads live in PostgreSQL.
"""


def generate_readme() -> str:
    """Generate README for the starter kit."""
    return """# Brain Starter Kit

A shareable set of learned capabilities for bootstrapping an AI agent's memory system.

## Contents

- `memories.jsonl` — Universal memories (lessons, principles, techniques) scrubbed of personal info
- `skills.json` — Skill definitions with maturity levels, pitfalls, and success rates
- `templates/` — optional private operator-context guidance

## Setup

### 1. Create the database
```bash
createdb illo_brain
python3 -m alembic upgrade head
```

### 2. Import the starter kit
```bash
cd /path/to/illo-brain
python3 -m brain.app.cli.brain_import --from-export /path/to/this/directory/
```

### 3. Configure private operator context
Review `templates/operator-context.md`, then create local files under `ILLO_PRIVATE_HOME` or `AGENT_CONTEXT_DIR` if you want personalized prompts/reminders.

## ⚠️ Privacy Note

These memories have been scrubbed through regex patterns and LLM rewriting to remove
identifying information. **However, human spot-check is strongly recommended before
publishing or sharing this kit.** Automated scrubbing may miss contextual identifiers
or paraphrased personal details.

Review `memories.jsonl` line by line before distributing.

## License

These learned capabilities are provided as-is. The schema and tooling are part of
the Illo Brain project.
"""


def run_export(scope: str, output_dir: str, dry_run: bool = False,
               skip_llm: bool = False):
    """Run the full export pipeline."""
    print(f"Exporting {scope} memories to {output_dir}...")

    # Memories
    memories = export_memories(scope, skip_llm=skip_llm)
    print(f"  {len(memories)} memories to export")

    # Skills
    skills = export_skills()
    print(f"  {len(skills)} skills to export")

    if dry_run:
        print("\n[DRY RUN] Would export:")
        print(f"  {len(memories)} memories")
        print(f"  {len(skills)} skills")
        for m in memories[:5]:
            print(f"    [{m['memory_type']}] {m['content'][:80]}...")
        return

    # Create output structure
    os.makedirs(output_dir, exist_ok=True)
    os.makedirs(os.path.join(output_dir, 'templates'), exist_ok=True)

    # Write memories.jsonl
    with open(os.path.join(output_dir, 'memories.jsonl'), 'w') as f:
        for m in memories:
            # Convert any non-serializable types
            clean = {k: (str(v) if not isinstance(v, (str, int, float, list, type(None), bool)) else v)
                     for k, v in m.items()}
            f.write(json.dumps(clean) + '\n')

    # Write skills.json
    with open(os.path.join(output_dir, 'skills.json'), 'w') as f:
        # Convert pitfalls/refinements/triggers from jsonb
        for s in skills:
            for key in ('pitfalls', 'refinements', 'triggers'):
                if isinstance(s[key], str):
                    try:
                        s[key] = json.loads(s[key])
                    except (json.JSONDecodeError, TypeError):
                        pass
        json.dump(skills, f, indent=2, default=str)

    # Write templates. These are documentation-only; personalized prompt files
    # should live outside source control under ILLO_PRIVATE_HOME/agent-context.
    templates = {
        'operator-context.md': generate_operator_context_readme(),
    }
    for filename, content in templates.items():
        with open(os.path.join(output_dir, 'templates', filename), 'w') as f:
            f.write(content)

    # Write README
    with open(os.path.join(output_dir, 'README.md'), 'w') as f:
        f.write(generate_readme())

    print(f"\nExport complete: {output_dir}/")
    print(f"  memories.jsonl: {len(memories)} memories")
    print(f"  skills.json: {len(skills)} skills")
    print(f"  templates/: {len(templates)} files")
    print(f"  README.md: ✓")
    print(f"\n⚠️  Human spot-check of memories.jsonl recommended before sharing.")


def main():
    parser = argparse.ArgumentParser(description='Export brain as starter kit')
    parser.add_argument('--scope', default='universal', choices=['universal', 'personal', 'all'])
    parser.add_argument('--output', default='./export', help='Output directory')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--skip-llm', action='store_true',
                        help='Skip LLM scrubbing pass (regex only)')
    args = parser.parse_args()

    run_export(args.scope, args.output, dry_run=args.dry_run, skip_llm=args.skip_llm)


if __name__ == '__main__':
    main()
